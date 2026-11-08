"""混合检索（S3）：FTS5(jieba 分词) + numpy 稠密向量 + RRF 融合。

两条召回通道互补：

- **FTS5**：科目名、公司名、代码这类精确词；jieba 分词后按词建索引，2 字词也能命中；
- **稠密**：不含专有名词的定性问题（「公司怎么描述海外业务风险」）。

融合用 RRF（Reciprocal Rank Fusion），不比较两路分数量纲；任何一路缺失都能降级工作。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

import numpy as np

from finkb.fact_engine.selector import FINKB_ROOT
from finkb.search.embed import Embedder

CHUNKS_PATH = FINKB_ROOT / "data" / "_search" / "chunks.jsonl"
DB_PATH = FINKB_ROOT / "data" / "_search" / "chunks.db"
VECTORS_PATH = FINKB_ROOT / "data" / "_search" / "vectors.npy"
RRF_K = 60
FILTER_FIELDS = ("company", "period", "doc_id", "block_type", "report_type")

# 问句里的功能词不承载证据；留着只会让「如何/描述」把审计报告顶到第一。
STOPWORDS = frozenset(
    "如何 怎么 怎样 什么 哪些 哪个 为什么 是否 多少 请问 请 描述 介绍 说明 一下 情况 变化 "
    "关于 对于 根据 相关 主要 分别 以及 还有 公司 需要 可以 我们 他们 这个 那个 这些 那些 "
    "其中 目前 现在 何时 哪里 有没有 能否 可否 告诉 看看 帮我 一个 进行 存在 表示 认为 "
    "披露 包括 内容 方面 有关 具体 相关 情况 时候".split()
)


def _content_tokens(text: str) -> list[str]:
    """用于打分的内容词：去单字、去功能词。"""
    return [token for token in tokenize(text) if len(token) >= 2 and token not in STOPWORDS]


def _doc_report_type(doc_id: str) -> str:
    return str(doc_id).rsplit("_", 1)[-1] if "_" in str(doc_id) else ""


def tokenize(text: str) -> list[str]:
    """jieba 分词；失败时退化为字二元组，保证检索仍可用。"""
    text = (text or "").strip()
    if not text:
        return []
    try:
        import jieba

        tokens = [token.strip() for token in jieba.lcut(text)]
    except Exception:  # pragma: no cover - jieba 缺失时的兜底
        tokens = [text[i : i + 2] for i in range(max(len(text) - 1, 1))]
    return [token for token in tokens if token and not token.isspace()]


def load_chunks(path: str | Path | None = None) -> list[dict]:
    file = Path(path or CHUNKS_PATH)
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]


def _embed_resumable(chunks: list[dict], embedder: Embedder, parts_dir: Path) -> np.ndarray:
    """分批向量化并落盘；中断后重跑会跳过已有分片。"""
    parts_dir.mkdir(parents=True, exist_ok=True)
    texts = [chunk["text"] for chunk in chunks]
    batch = int(getattr(embedder, "batch_size", 64) or 64)
    vectors: list[np.ndarray] = []
    for start in range(0, len(texts), batch):
        part = parts_dir / f"part_{start:06d}.npy"
        if part.exists():
            vectors.append(np.load(part))
            continue
        block = np.asarray(embedder.embed(texts[start : start + batch]), dtype=np.float32)
        np.save(part, block)
        vectors.append(block)
        if (start // batch) % 20 == 0:
            print(f"embedded {start + len(block)}/{len(texts)}", flush=True)
    return np.vstack(vectors)


def _text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _embed_incremental(
    chunks: list[dict], embedder: Embedder, target: Path, manifest_path: Path
) -> tuple[np.ndarray, dict, int] | None:
    """只对新增/变化的块算向量，其余按 chunk_id + 文本哈希复用旧向量。

    返回 None 表示没有可用的旧索引（需要全量重算）。
    """
    if not (target.exists() and manifest_path.exists()):
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ids = manifest.get("ids") or []
    hashes = manifest.get("hashes") or []
    dims = int(manifest.get("dims") or 0)
    if len(ids) != len(hashes) or dims != int(getattr(embedder, "dims", 0) or 0):
        return None
    model = getattr(embedder, "model", None)
    if model and manifest.get("model") and manifest["model"] != model:
        return None
    old = np.load(target, mmap_mode="r")
    if old.shape[0] != len(ids) or old.shape[1] != dims:
        return None
    index = {chunk_id: position for position, chunk_id in enumerate(ids)}
    rows = np.empty((len(chunks), dims), dtype=np.float32)
    missing: list[int] = []
    new_hashes: list[str] = []
    for position, chunk in enumerate(chunks):
        text_hash = _text_hash(chunk["text"])
        new_hashes.append(text_hash)
        previous = index.get(chunk["chunk_id"])
        if previous is not None and hashes[previous] == text_hash:
            rows[position] = old[previous]
        else:
            missing.append(position)
    if missing:
        texts = [chunks[position]["text"] for position in missing]
        batch = int(getattr(embedder, "batch_size", 64) or 64)
        done = 0
        reused = len(chunks) - len(missing)
        for start in range(0, len(texts), batch):
            block = np.asarray(embedder.embed(texts[start : start + batch]), dtype=np.float32)
            for offset, vector in enumerate(block):
                rows[missing[start + offset]] = vector
            done += len(block)
            if (start // batch) % 20 == 0:
                print(f"embedded {done}/{len(texts)}（复用 {reused} 块）", flush=True)
    return (
        rows,
        {"ids": [chunk["chunk_id"] for chunk in chunks], "hashes": new_hashes, "dims": dims, "model": model or ""},
        len(chunks) - len(missing),
    )


def build_index(
    chunks_path: str | Path | None = None,
    db_path: str | Path | None = None,
    vectors_path: str | Path | None = None,
    embedder: Embedder | None = None,
    limit: int | None = None,
    incremental: bool = True,
) -> dict:
    """建 FTS5 索引；给了 embedder 就同时算稠密向量。

    ``incremental=True`` 时，和旧 ``vectors.npy`` + 清单比对，只算新增/改文本的块；
    旧清单缺失、维度/模型不一致时自动退回全量。
    """
    chunks = load_chunks(chunks_path)
    if limit:
        chunks = chunks[:limit]
    db = Path(db_path or DB_PATH)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        DROP TABLE IF EXISTS chunks;
        DROP TABLE IF EXISTS chunks_fts;
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY, doc_id TEXT, company TEXT, period TEXT,
            report_type TEXT, page_num INTEGER, section TEXT, block_type TEXT,
            table_header TEXT, text TEXT
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            chunk_id UNINDEXED, section, table_header, body, tokenize='unicode61'
        );
        """
    )
    chunk_rows = [
        (
            chunk["chunk_id"], chunk["doc_id"], chunk["company"], chunk["period"],
            _doc_report_type(chunk["doc_id"]), int(chunk["page_num"]),
            chunk.get("section", ""), chunk["block_type"], chunk.get("table_header", ""), chunk["text"],
        )
        for chunk in chunks
    ]
    fts_rows = [
        (
            chunk["chunk_id"],
            " ".join(_content_tokens(chunk.get("section", ""))),
            " ".join(_content_tokens(chunk.get("table_header", ""))),
            " ".join(_content_tokens(chunk["text"])),
        )
        for chunk in chunks
    ]
    conn.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)", chunk_rows)
    conn.executemany(
        "INSERT INTO chunks_fts (chunk_id, section, table_header, body) VALUES (?,?,?,?)", fts_rows
    )
    df: dict[str, int] = {}
    for _, section, header, body in fts_rows:
        for token in set((section + " " + header + " " + body).split()):
            df[token] = df.get(token, 0) + 1
    conn.execute("DROP TABLE IF EXISTS term_df")
    conn.execute("CREATE TABLE term_df (token TEXT PRIMARY KEY, df INTEGER)")
    conn.executemany("INSERT INTO term_df (token, df) VALUES (?,?)", df.items())
    conn.commit()
    conn.close()
    stats = {"chunks": len(chunks), "db": str(db)}
    if embedder is not None:
        target = Path(vectors_path or VECTORS_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = target.parent / "vectors_manifest.json"
        reused = 0
        if incremental:
            built = _embed_incremental(chunks, embedder, target, manifest_path)
            if built is not None:
                vectors, manifest, reused = built
            else:
                # 有旧 vectors 却没有清单：分片可能对应旧 chunks 顺序，不能复用
                if target.exists():
                    shutil.rmtree(target.parent / "vectors_parts", ignore_errors=True)
                vectors = _embed_resumable(chunks, embedder, target.parent / "vectors_parts")
                manifest = {
                    "ids": [chunk["chunk_id"] for chunk in chunks],
                    "hashes": [_text_hash(chunk["text"]) for chunk in chunks],
                    "dims": int(vectors.shape[1]) if vectors.size else 0,
                    "model": getattr(embedder, "model", "") or "",
                }
        else:
            shutil.rmtree(target.parent / "vectors_parts", ignore_errors=True)
            vectors = _embed_resumable(chunks, embedder, target.parent / "vectors_parts")
            manifest = {
                "ids": [chunk["chunk_id"] for chunk in chunks],
                "hashes": [_text_hash(chunk["text"]) for chunk in chunks],
                "dims": int(vectors.shape[1]) if vectors.size else 0,
                "model": getattr(embedder, "model", "") or "",
            }
        np.save(target, vectors)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        stats.update({"vectors": str(target), "dims": int(vectors.shape[1]), "reused": reused})
    return stats


def _where(filters: dict | None) -> tuple[str, list]:
    clauses, params = [], []
    for field in FILTER_FIELDS:
        value = (filters or {}).get(field)
        if value:
            clauses.append(f"c.{field} = ?")
            params.append(value)
    if (filters or {}).get("section"):
        clauses.append("c.section LIKE ?")
        params.append(f"%{filters['section']}%")
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _ngrams(question: str) -> list[str]:
    """问句里的连续中文片段取 3–4 字 n-gram，作为「短语命中」的额外信号。

    分词不一致时（问句切出「银行业务」、正文切出「投资银行」），词项对不上；
    但「投资银」「银行业」这类字面片段仍能命中，且罕见片段权重高，能压住
    公司名/表格里反复出现的通用词。
    """
    grams: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]{3,}", question or ""):
        for size in (3, 4):
            for index in range(len(run) - size + 1):
                gram = run[index : index + size]
                if gram not in STOPWORDS:
                    grams.append(gram)
    return list(dict.fromkeys(grams))


def _term_search(
    chunks: list[dict],
    query: str,
    limit: int,
    filters: dict | None,
) -> list[tuple[str, float]]:
    """词项打分：直接在过滤后的块集合上算 tf-idf，不依赖分词结果与索引完全一致。

    长问句里每个词单独看相关度；权重用**当前过滤子集**内的文档频率（查某家公司时，
    公司名在它自己的文档里到处都是，按全局 df 会盖住「投资银行业务」这类真信号）；
    章节标题命中权重更高（×3），因为「环境风险」「产能状况」常写在标题里。
    """
    tokens = list(dict.fromkeys(_content_tokens(query)))
    if not tokens:
        return []
    phrases = _ngrams(query)
    all_tokens = tokens + [gram for gram in phrases if gram not in tokens]
    subset = [chunk for chunk in chunks if _passes(chunk, filters)]
    if not subset:
        return []
    rows: list[tuple[dict, dict[str, int]]] = []
    for chunk in subset:
        text, section, header = chunk.get("text") or "", chunk.get("section") or "", chunk.get("table_header") or ""
        tf: dict[str, int] = {}
        for token in all_tokens:
            count = text.count(token) + 3 * section.count(token) + 2 * header.count(token)
            if count:
                tf[token] = count
        if tf:
            rows.append((chunk, tf))
    if not rows:
        return []
    total = len(subset)
    df = {token: sum(1 for _, tf in rows if token in tf) for token in all_tokens}
    weights = {token: math.log((total + 1) / (df[token] + 0.5)) for token in all_tokens}
    phrase_set = set(phrases)
    scores: dict[str, float] = {}
    for chunk, tf in rows:
        score = sum(
            weights[token] * (1.0 + math.log1p(min(count, 3))) * (0.2 if token in phrase_set else 1.0)
            for token, count in tf.items()
        )
        if score > 0:
            scores[chunk["chunk_id"]] = score
    return sorted(scores.items(), key=lambda item: -item[1])[:limit]


def _passes(chunk: dict, filters: dict | None) -> bool:
    for field in FILTER_FIELDS:
        value = (filters or {}).get(field)
        if not value:
            continue
        actual = _doc_report_type(chunk.get("doc_id", "")) if field == "report_type" else chunk.get(field)
        if str(actual) != str(value):
            return False
    section = (filters or {}).get("section")
    return not section or section in str(chunk.get("section", ""))


def _dense_search(
    chunks: list[dict], vectors: np.ndarray, query_vector: np.ndarray, limit: int, filters: dict | None
) -> list[tuple[str, float]]:
    if vectors.shape[0] != len(chunks):
        return []
    scores = vectors @ query_vector.astype(np.float32)
    order = np.argsort(-scores)
    results: list[tuple[str, float]] = []
    for index in order:
        chunk = chunks[int(index)]
        if not _passes(chunk, filters):
            continue
        results.append((chunk["chunk_id"], float(scores[int(index)])))
        if len(results) >= limit:
            break
    return results


def search(
    query: str,
    *,
    limit: int = 10,
    filters: dict | None = None,
    db_path: str | Path | None = None,
    chunks_path: str | Path | None = None,
    vectors_path: str | Path | None = None,
    embedder: Embedder | None = None,
    chunks: list[dict] | None = None,
    vectors: np.ndarray | None = None,
) -> list[dict]:
    """混合检索；返回按 RRF 分数降序的块。"""
    if chunks is None:
        chunks = load_chunks(chunks_path)
    by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
    fts_results = _term_search(chunks, query, limit * 3, filters)
    dense_results: list[tuple[str, float]] = []
    vectors_file = Path(vectors_path or VECTORS_PATH)
    if vectors is None and vectors_file.exists():
        vectors = np.load(vectors_file)
    if embedder is not None and vectors is not None:
        query_vector = np.asarray(embedder.embed([query])[0], dtype=np.float32)
        dense_results = _dense_search(chunks, vectors, query_vector, limit * 3, filters)

    scores: dict[str, dict] = {}
    for rank, (chunk_id, score) in enumerate(fts_results, start=1):
        entry = scores.setdefault(chunk_id, {"rrf": 0.0, "channels": []})
        entry["rrf"] += 1.0 / (RRF_K + rank)
        entry["channels"].append("fts")
        entry["fts_score"] = score
        entry["fts_rank"] = rank
    for rank, (chunk_id, score) in enumerate(dense_results, start=1):
        entry = scores.setdefault(chunk_id, {"rrf": 0.0, "channels": []})
        entry["rrf"] += 1.0 / (RRF_K + rank)
        entry["channels"].append("dense")
        entry["dense_score"] = score
        entry["dense_rank"] = rank

    results: list[dict] = []
    for chunk_id, entry in sorted(scores.items(), key=lambda item: -item[1]["rrf"])[:limit]:
        chunk = dict(by_id.get(chunk_id, {"chunk_id": chunk_id}))
        chunk.update(entry)
        results.append(chunk)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="混合检索（FTS5 + 稠密 + RRF）")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--chunks", default=None)
    build.add_argument("--db", default=None)
    build.add_argument("--vectors", default=None)
    build.add_argument("--no-dense", action="store_true")
    build.add_argument("--limit", type=int, default=None)
    query = sub.add_parser("query")
    query.add_argument("query")
    query.add_argument("--limit", type=int, default=10)
    query.add_argument("--company", default=None)
    query.add_argument("--period", default=None)
    query.add_argument("--doc", default=None)
    query.add_argument("--type", dest="block_type", default=None)
    query.add_argument("--report-type", dest="report_type", default=None)
    args = parser.parse_args()

    if args.command == "build":
        embedder = None
        if not args.no_dense:
            from finkb.search.embed import SiliconFlowEmbedder

            embedder = SiliconFlowEmbedder()
        print(json.dumps(build_index(args.chunks, args.db, args.vectors, embedder, args.limit), ensure_ascii=False))
        return
    filters = {
        "company": args.company, "period": args.period, "doc_id": args.doc,
        "block_type": args.block_type, "report_type": args.report_type,
    }
    from finkb.search.embed import SiliconFlowEmbedder

    try:
        embedder = SiliconFlowEmbedder()
    except Exception:
        embedder = None
    results = search(args.query, limit=args.limit, filters=filters, embedder=embedder)
    for item in results:
        print(json.dumps(
            {key: item.get(key) for key in ("chunk_id", "doc_id", "page_num", "section", "block_type", "rrf", "channels")},
            ensure_ascii=False,
        ))


if __name__ == "__main__":
    main()
