"""文字题召回评测（P1-3）：FTS-only / FTS+稠密+RRF，含元数据过滤；报 Recall@k 与 MRR@k。

``--no-dense`` 时不加载向量模型，纯离线跑（check.sh 可选用）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FINKB = ROOT / "finkb"
sys.path.insert(0, str(FINKB / "src"))

from finkb.search import hybrid  # noqa: E402
from finkb.search.embed import SiliconFlowEmbedder  # noqa: E402
from finkb.qa import fact_lane  # noqa: E402

QA_PATH = ROOT / "eval" / "text_qa.json"
CHUNKS_PATH = FINKB / "data" / "_search" / "chunks.jsonl"
DB_PATH = FINKB / "data" / "_search" / "chunks.db"
VECTORS_PATH = FINKB / "data" / "_search" / "vectors.npy"
SCOPE_PATH = FINKB / "config" / "wiki_scope.yaml"


def load_scope() -> dict[str, str]:
    import yaml

    scope = yaml.safe_load(SCOPE_PATH.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for doc in scope["docs"]:
        mapping[doc["company_name"]] = doc["company"]
        mapping[doc["doc_id"].split("_")[0]] = doc["company"]
    return mapping


def reciprocal_rank(results: list[dict], expected: list[dict]) -> float:
    """第一个命中页出现在第几位；没命中返回 0。"""
    for rank, result in enumerate(results, start=1):
        for item in expected:
            if result.get("doc_id") == item["doc_id"] and result.get("page_num") in item["pages"]:
                return 1.0 / rank
    return 0.0


def is_hit(results: list[dict], expected: list[dict]) -> bool:
    return reciprocal_rank(results, expected) > 0


def _filters(question: str, scope: dict[str, str]) -> dict[str, str]:
    company = next((code for name, code in scope.items() if name and name in question), None)
    slot = fact_lane.extract(question)
    filters: dict[str, str] = {}
    if company:
        filters["company"] = company
    if slot.periods:
        filters["period"] = slot.periods[0]
    return filters


def main() -> None:
    parser = argparse.ArgumentParser(description="文字题 Recall@k / MRR@k 评测")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--no-dense", action="store_true", help="只用 FTS，不连 embedding API")
    parser.add_argument("--min-recall", type=float, default=None, help="过滤后 Recall 低于该值时退出 1")
    args = parser.parse_args()

    data = json.loads(QA_PATH.read_text(encoding="utf-8"))
    questions = data["questions"]
    scope = load_scope()
    chunks = hybrid.load_chunks(CHUNKS_PATH)

    vectors = None
    embedder = None
    if not args.no_dense:
        import numpy as np

        vectors = np.load(VECTORS_PATH)
        embedder = SiliconFlowEmbedder()

    configs = ["fts", "fts_filtered"] + ([] if args.no_dense else ["hybrid", "hybrid_filtered"])
    stats = {name: {"recall": 0, "mrr": 0.0} for name in configs}
    misses: list[str] = []

    for item in questions:
        question = item["question"]
        filters = _filters(question, scope) or None
        results = {
            "fts": hybrid.search(question, limit=args.limit, chunks=chunks, db_path=DB_PATH),
            "fts_filtered": hybrid.search(question, limit=args.limit, chunks=chunks, db_path=DB_PATH,
                                          filters=filters),
        }
        if not args.no_dense:
            results["hybrid"] = hybrid.search(
                question, limit=args.limit, chunks=chunks, vectors=vectors, embedder=embedder,
                db_path=DB_PATH, vectors_path=VECTORS_PATH,
            )
            results["hybrid_filtered"] = hybrid.search(
                question, limit=args.limit, chunks=chunks, vectors=vectors, embedder=embedder,
                filters=filters, db_path=DB_PATH, vectors_path=VECTORS_PATH,
            )
        for name in configs:
            rr = reciprocal_rank(results[name], item["expected"])
            stats[name]["recall"] += int(rr > 0)
            stats[name]["mrr"] += rr
        if not is_hit(results["fts_filtered"], item["expected"]):
            misses.append(f"{item['id']} {question[:40]} filters={filters}")

    total = len(questions)
    report = {
        "total": total,
        "limit": args.limit,
        "dense": not args.no_dense,
        **{
            f"{name}_recall@{args.limit}": round(stats[name]["recall"] / total, 3)
            for name in configs
        },
        **{
            f"{name}_mrr@{args.limit}": round(stats[name]["mrr"] / total, 3)
            for name in configs
        },
        "misses": misses,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.min_recall is not None and report[f"fts_filtered_recall@{args.limit}"] < args.min_recall:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
