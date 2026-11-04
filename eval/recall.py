"""文字题 Recall@5 评测：FTS-only 与 FTS+稠密+RRF，含元数据过滤。"""

from __future__ import annotations

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


def is_hit(results: list[dict], expected: list[dict]) -> bool:
    for result in results:
        for item in expected:
            if result.get("doc_id") == item["doc_id"] and result.get("page_num") in item["pages"]:
                return True
    return False


def main() -> None:
    data = json.loads(QA_PATH.read_text(encoding="utf-8"))
    questions = data["questions"]
    scope = load_scope()
    chunks = hybrid.load_chunks(CHUNKS_PATH)
    try:
        import numpy as np

        vectors = np.load(VECTORS_PATH)
    except Exception:
        vectors = None
    embedder = SiliconFlowEmbedder()

    stats = {"fts": 0, "fts_filtered": 0, "hybrid": 0, "hybrid_filtered": 0}
    misses: list[str] = []
    for item in questions:
        question = item["question"]
        company = next((code for name, code in scope.items() if name and name in question), None)
        slot = fact_lane.extract(question)
        filters = {"company": company} if company else {}
        if slot.periods:
            filters["period"] = slot.periods[0]
        fts = hybrid.search(question, limit=5, chunks=chunks, db_path=DB_PATH, vectors_path=VECTORS_PATH)
        fts_filtered = hybrid.search(
            question, limit=5, chunks=chunks, db_path=DB_PATH, vectors_path=VECTORS_PATH,
            filters=filters or None,
        )
        hybrid_results = hybrid.search(
            question, limit=5, chunks=chunks, vectors=vectors, embedder=embedder,
            db_path=DB_PATH, vectors_path=VECTORS_PATH,
        )
        filtered = hybrid.search(
            question, limit=5, chunks=chunks, vectors=vectors, embedder=embedder,
            filters=filters or None,
            db_path=DB_PATH, vectors_path=VECTORS_PATH,
        )
        hit_fts = is_hit(fts, item["expected"])
        hit_fts_filtered = is_hit(fts_filtered, item["expected"])
        hit_hybrid = is_hit(hybrid_results, item["expected"])
        hit_filtered = is_hit(filtered, item["expected"])
        stats["fts"] += int(hit_fts)
        stats["fts_filtered"] += int(hit_fts_filtered)
        stats["hybrid"] += int(hit_hybrid)
        stats["hybrid_filtered"] += int(hit_filtered)
        if not hit_filtered:
            misses.append(f"{item['id']} {question[:40]} company={company}")
    total = len(questions)
    print(json.dumps({
        "total": total,
        "fts_recall@5": round(stats["fts"] / total, 3),
        "fts_filtered_recall@5": round(stats["fts_filtered"] / total, 3),
        "hybrid_recall@5": round(stats["hybrid"] / total, 3),
        "hybrid_filtered_recall@5": round(stats["hybrid_filtered"] / total, 3),
        "misses": misses,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
