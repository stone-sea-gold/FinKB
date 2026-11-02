#!/usr/bin/env python3
"""每份文档的事实数底线（坑-80 的门禁版）。

重灌是破坏性操作：先把 facts 表清空再重写。如果某份文档在抽取阶段抛了异常，
`ingest.run()` 会把它记成 `{"error": ...}` 塞进返回值——进程照样退出 0，聚合总数
反而因为新增指标而增长，于是「一份文档 0 事实」被完全掩盖。601238 就是这样丢了
68 条事实，直到人工抽查按文档对账才发现。

本工具把「每份文档至少有多少条事实」变成可比的基线：--update 记录当前值，
--check 与基线比对，任何文档归零或腰斩即失败。

用法：
    ../.venv/bin/python tools/facts_baseline.py --update
    ../.venv/bin/python tools/facts_baseline.py --check

退出码：0 通过；1 有文档低于底线。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

DEFAULT_BASELINE = Path("config/facts-baseline.json")
# 低于基线的这个比例就判失败。留 20% 余量给「口径调整后某些行不再命中指标」这类正常波动，
# 但抓不住的话说明波动本身已经大到该看一眼了。
FLOOR_RATIO = 0.8


def counts(db: Path) -> dict[str, int]:
    conn = duckdb.connect(str(db), read_only=True)
    rows = conn.execute(
        "SELECT source_doc, count(*) FROM facts GROUP BY source_doc ORDER BY source_doc"
    ).fetchall()
    conn.close()
    return {doc: n for doc, n in rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/facts.duckdb")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--update", action="store_true", help="用当前库覆盖基线")
    parser.add_argument("--check", action="store_true", help="与基线比对")
    args = parser.parse_args()

    db = Path(args.db)
    baseline_path = Path(args.baseline)
    if not db.exists():
        print(f"跳过：{db} 不存在（先跑全量重灌）")
        return 0

    actual = counts(db)

    if args.update:
        baseline_path.write_text(
            json.dumps({"docs": actual}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已写基线 {baseline_path}（{len(actual)} 份文档 / {sum(actual.values())} 条事实）")
        return 0

    if not baseline_path.exists():
        print(f"跳过：{baseline_path} 不存在（先 --update 生成一次基线）")
        return 0

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["docs"]
    failures: list[str] = []
    for doc, base in baseline.items():
        got = actual.get(doc, 0)
        if got < base * FLOOR_RATIO:
            failures.append(f"{doc}: {base} → {got}" + ("（整份消失）" if got == 0 else ""))

    missing = sorted(set(actual) - set(baseline))
    if missing:
        print(f"新文档（基线里还没有，允许）：{', '.join(missing)}")
    if failures:
        print("事实数低于底线：")
        for line in failures:
            print(f"  - {line}")
        print(f"\n共 {len(failures)} 份文档掉产。重灌日志里找这些文档的 error 条目。")
        return 1

    print(f"每份文档事实数 ≥ 基线 {FLOOR_RATIO:.0%}：{len(baseline)} 份通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
