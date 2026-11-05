#!/usr/bin/env python3
"""指标治理检查 CLI（ADR-060）。

用法（在 finkb/ 下）：
    ../.venv/bin/python tools/metric_check.py --db data/facts.duckdb
    ../.venv/bin/python tools/metric_check.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finkb.fact_engine.metric_governance import (  # noqa: E402
    DEFAULT_DB,
    DEFAULT_DICT,
    DEFAULT_METRICS,
    DEFAULT_ONTOLOGY,
    DEFAULT_QUERIES,
    audit,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="检查 Tier 1 指标清单（只读）")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--metrics", default=str(DEFAULT_METRICS))
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--dict", dest="dict_path", default=str(DEFAULT_DICT))
    parser.add_argument("--ontology", default=str(DEFAULT_ONTOLOGY))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-db", action="store_true", help="只查配置，不连事实库")
    args = parser.parse_args()

    result = audit(
        db_path=args.db,
        metrics_path=args.metrics,
        queries_path=args.queries,
        dict_path=args.dict_path,
        ontology_path=args.ontology,
        check_db=not args.no_db,
    )
    payload = result.as_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"指标治理：{'通过' if result.ok else '未通过'}")
        print(f"  Tier 1：直接披露 {result.governed.get('raw', 0)} 个 · 命名 SQL {result.governed.get('query', 0)} 个")
        print(f"  未治理（pending）：{result.governed.get('pending', 0)} 个")
        print(f"  SQL 检查：{result.sql_checked} 条；连库：{'是' if result.db_checked else '否'}")
        for error in result.hard_errors:
            print(f"  [错误] {error}")
        for warning in result.warnings:
            print(f"  [警告] {warning}")
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
