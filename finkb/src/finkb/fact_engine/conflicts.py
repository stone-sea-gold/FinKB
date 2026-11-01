"""facts 级跨文档冲突检测（ADR-046）。

同一 ``company + metric_id + period + caliber`` 在不同文档里数值不一致时记一条
冲突。只比数值，不做叙述语义冲突（那需要 LLM 判断，留给 L3）。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from finkb.fact_engine.selector import FINKB_ROOT
from finkb.fact_engine.store import FactStore

TOLERANCE = 1e-6


def detect_conflicts(store: FactStore) -> int:
    """清空并重算 conflicts 表，返回冲突组数。"""
    store.conn.execute("DELETE FROM conflicts")
    rows = store.conn.execute(
        """
        SELECT a.company, a.metric_id, a.period, a.caliber,
               a.fact_id, b.fact_id, a.value_norm, b.value_norm,
               a.source_doc, b.source_doc
        FROM facts a JOIN facts b
          ON a.company = b.company
         AND a.metric_id = b.metric_id
         AND a.period = b.period
         AND a.caliber = b.caliber
         AND a.fact_id < b.fact_id
        WHERE abs(a.value_norm - b.value_norm)
              > ? * greatest(1.0, abs(a.value_norm), abs(b.value_norm))
        ORDER BY a.company, a.metric_id, a.period
        """,
        [TOLERANCE],
    ).fetchall()
    now = datetime.now().isoformat(timespec="seconds")
    for i, row in enumerate(rows, 1):
        store.conn.execute(
            "INSERT INTO conflicts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                f"C{i:04d}",
                *row[:4],
                row[4],
                row[5],
                row[6],
                row[7],
                row[8],
                row[9],
                now,
            ],
        )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="检测 facts 级跨文档冲突")
    parser.add_argument("--db", default=str(FINKB_ROOT / "data" / "facts.duckdb"))
    args = parser.parse_args()
    with FactStore(Path(args.db)) as store:
        count = detect_conflicts(store)
    print(f"conflicts: {count}")


if __name__ == "__main__":
    main()
