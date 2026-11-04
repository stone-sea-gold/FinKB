"""本体算子：把「概念 → 事实 → 原文页」串起来（S2 / S4）。

S2 只实现一个最小算子 ``note_drill_down``：给定公司/期间/指标，返回主表页锚与
附注章节页锚。它不生成任何数字（数字仍只由 SQL/事实表出），只负责「去哪一页看」。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from finkb.fact_engine.selector import FINKB_ROOT


def _rows(conn, sql: str, params: list) -> list[dict]:
    cursor = conn.execute(sql, params)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def note_drill_down(
    conn, company: str, period: str, metric_id: str, *, limit: int = 8
) -> dict:
    """返回该指标的报表页锚与附注页锚；附注缺失时 ``missing_note`` 为 True。"""
    statement = _rows(
        conn,
        "SELECT doc_id, page_num, section, title FROM note_anchors "
        "WHERE company = ? AND period = ? AND metric_id = ? AND anchor_type = 'statement' "
        "ORDER BY doc_id, page_num LIMIT ?",
        [company, period, metric_id, limit],
    )
    notes = _rows(
        conn,
        "SELECT doc_id, page_num, section, title FROM note_anchors "
        "WHERE company = ? AND period = ? AND metric_id = ? AND anchor_type = 'note' "
        "ORDER BY doc_id, page_num LIMIT ?",
        [company, period, metric_id, limit],
    )
    if not notes:
        # 半年报/季报可能没有完整附注，退回同公司同指标最近一期年报的附注锚。
        notes = _rows(
            conn,
            "SELECT doc_id, page_num, section, title FROM note_anchors "
            "WHERE company = ? AND metric_id = ? AND anchor_type = 'note' "
            "ORDER BY doc_id DESC, page_num LIMIT ?",
            [company, metric_id, limit],
        )
    return {
        "company": company,
        "period": period,
        "metric_id": metric_id,
        "statement": statement,
        "notes": notes,
        "missing_note": not notes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="本体算子（附注穿透）")
    parser.add_argument("--db", default=str(FINKB_ROOT / "data" / "facts.duckdb"))
    parser.add_argument("--company", required=True)
    parser.add_argument("--period", required=True)
    parser.add_argument("--metric", required=True)
    args = parser.parse_args()

    import duckdb

    conn = duckdb.connect(args.db, read_only=True)
    print(json.dumps(
        note_drill_down(conn, args.company, args.period, args.metric),
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    main()
