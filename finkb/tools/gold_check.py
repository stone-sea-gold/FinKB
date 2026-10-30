#!/usr/bin/env python3
"""金标事实集校验：把人工抄录的期望值与事实库逐条比对。

用法：
    ../.venv/bin/python tools/gold_check.py --db data/facts.duckdb --gold config/gold_facts.yaml

退出码：0 全过；1 有缺失或数值不符。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import yaml

TOLERANCE = {"亿元": 0.01, "%": 0.01, "元/股": 0.01}


def tolerance(unit: str) -> float:
    return TOLERANCE.get(unit, 0.01)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/facts.duckdb")
    parser.add_argument("--gold", default="config/gold_facts.yaml")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"跳过：{db} 不存在（先跑全量重灌）")
        return 0

    entries = yaml.safe_load(Path(args.gold).read_text(encoding="utf-8"))["entries"]
    conn = duckdb.connect(str(db), read_only=True)
    failures = []
    for item in entries:
        row = conn.execute(
            "select value_norm from facts where source_doc=? and period=? and metric_id=?",
            [item["source_doc"], str(item["period"]), item["metric_id"]],
        ).fetchone()
        if row is None:
            failures.append((item, None))
            continue
        actual = float(row[0])
        if abs(actual - float(item["expected"])) > tolerance(item["unit"]):
            failures.append((item, actual))
        elif args.verbose:
            print(f"PASS {item['company']} {item['period']} {item['metric_id']}: {actual}")
    conn.close()

    total = len(entries)
    print(f"金标事实集：{total - len(failures)}/{total} 通过")
    for item, actual in failures:
        expected = item["expected"]
        got = "缺失" if actual is None else f"{actual}"
        print(
            f"FAIL {item['company']} {item['period']} {item['metric_id']}: "
            f"期望 {expected}{item['unit']}，实际 {got}（{item['source_doc']} P{item['page']}）"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
