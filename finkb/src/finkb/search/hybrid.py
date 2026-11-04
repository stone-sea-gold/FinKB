"""混合检索（S3）：FTS5(jieba 分词) + numpy 稠密向量 + RRF 融合。

两条召回通道互补：

- **FTS5**：科目名、公司名、代码这类精确词；jieba 分词后按词建索引，2 字词也能命中；
- **稠密**：不含专有名词的定性问题（「公司怎么描述海外业务风险」）。

融合用 RRF（Reciprocal Rank Fusion），不比较两路分数量纲；任何一路缺失都能降级工作。
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path

import numpy as np

from finkb.fact_engine.selector import FINKB_ROOT
from finkb.search.embed import Embedder

CHUNKS_PATH = FINKB_ROOT / "data" / "_search" / "chunks.jsonl"
DB_PATH = FINKB_ROOT / "data" / "_search" / "chunks.db"
VECTORS_PATH = FINKB_ROOT / "data" / "_search" / "vectors.npy"
RRF_K = 60
FILTER_FIELDS = ("company", "period", "doc_id", "block_type")


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


def build_index(
    chunks_path: str | Path | None = None,
    db_path: str | Path | None = None,
    vectors_path: str | Path | None = None,
    embedder: Embedder | None = None,
    limit: int | None = None,
) -> dict:
    """建 FTS5 索引；给了 embedder 就同时算稠密向量。"""
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
            page_num INTEGER, section TEXT, block_type TEXT, text TEXT
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            chunk_id UNINDEXED, tokens, tokenize='unicode61'
        );
        """
    )
    chunk_rows = [
        (
            chunk["chunk_id"], chunk["doc_id"], chunk["company"], chunk["period"],
            int(chunk["page_num"]), chunk.get("section", ""), chunk["block_type"], chunk["text"],
        )
        for chunk in chunks
    ]
    fts_rows = [
        (chunk["chunk_id"], " ".join(tokenize(chunk["text"]))) for chunk in chunks
    ]
    conn.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)", chunk_rows)
    conn.executemany("INSERT INTO chunks_fts (chunk_id, tokens) VALUES (?,?)", fts_rows)
    df: dict[str, int] = {}
    for _, tokens in fts_rows:
        for token in set(tokens.split()):
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
        vectors = _embed_resumable(chunks, embedder, target.parent / "vectors_parts")
        np.save(target, vectors)
        stats.update({"vectors": str(target), "dims": int(vectors.shape[1])})
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


def _fts_search(conn, query: str, limit: int, filters: dict | None) -> list[tuple[str, float]]:
    tokens = [token for token in tokenize(query) if len(token) >= 2]
    if not tokens:
        return []
    total = conn.execute("SELECT count(*) FROM chunks").fetchone()[0] or 1
    df_map = dict(conn.execute("SELECT token, df FROM term_df").fetchall())
    where, params = _where(filters)
    scores: dict[str, float] = {}
    for token in dict.fromkeys(tokens):
        weight = math.log((total + 1) / (df_map.get(token, 0) + 1))
        if weight <= 0:
            continue
        rows = conn.execute(
            f"SELECT c.chunk_id FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id "
            f"WHERE chunks_fts MATCH ?{where} LIMIT ?",
            [f'"{token}"', *params, limit * 3],
        ).fetchall()
        for rank, (chunk_id,) in enumerate(rows, start=1):
            scores[str(chunk_id)] = scores.get(str(chunk_id), 0.0) + weight / (RRF_K + rank)
    return sorted(scores.items(), key=lambda item: -item[1])[:limit]


def _passes(chunk: dict, filters: dict | None) -> bool:
    for field in FILTER_FIELDS:
        value = (filters or {}).get(field)
        if value and str(chunk.get(field)) != str(value):
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
    fts_results: list[tuple[str, float]] = []
    db = Path(db_path or DB_PATH)
    if db.exists():
        conn = sqlite3.connect(db)
        try:
            fts_results = _fts_search(conn, query, limit * 3, filters)
        finally:
            conn.close()
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
    args = parser.parse_args()

    if args.command == "build":
        embedder = None
        if not args.no_dense:
            from finkb.search.embed import SiliconFlowEmbedder

            embedder = SiliconFlowEmbedder()
        print(json.dumps(build_index(args.chunks, args.db, args.vectors, embedder, args.limit), ensure_ascii=False))
        return
    filters = {"company": args.company, "period": args.period, "doc_id": args.doc, "block_type": args.block_type}
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
