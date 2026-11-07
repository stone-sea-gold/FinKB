"""跨期对比验收（6 道，离线）：期间归一、并排行、缺失行、拒答。

检查项：是否拒答、期间顺序、行数、数值/引用是否齐全、文案关键词、
响应契约字段（schema_version/disclaimer）。数字只来自事实库 SQL，不连网络。

用法：``.venv/bin/python eval/check_compare.py [--case P01]``
退出码：0=全过；1=有失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "finkb" / "src"))

from finkb.qa.compare import compare  # noqa: E402

EVAL_PATH = ROOT / "eval" / "compare_qa.json"


def check(payload: dict, expect: dict) -> list[str]:
    errors: list[str] = []
    if "refused" in expect and bool(payload.get("refused")) != bool(expect["refused"]):
        errors.append(f"refused={payload.get('refused')} 期望 {expect['refused']}")

    data = payload.get("data") or {}
    if "periods" in expect and data.get("periods") != expect["periods"]:
        errors.append(f"periods={data.get('periods')} 期望 {expect['periods']}")

    rows = data.get("rows") or []
    if "rows" in expect and len(rows) != expect["rows"]:
        errors.append(f"rows={len(rows)} 期望 {expect['rows']}")
    if expect.get("values_present"):
        missing = [row["period"] for row in rows if row.get("value") is None]
        if missing:
            errors.append(f"这些期间没有数值：{missing}")
        if len(payload.get("citations") or []) != len(rows):
            errors.append(f"citations={len(payload.get('citations') or [])} 期望 {len(rows)}")
        if len(payload.get("ledger") or []) != len(rows):
            errors.append(f"ledger={len(payload.get('ledger') or [])} 期望 {len(rows)}")

    text = payload.get("text") or ""
    for token in expect.get("text_contains") or []:
        if token not in text:
            errors.append(f"text 缺「{token}」：{text[:100]}")

    if not payload.get("refused"):
        if not payload.get("schema_version"):
            errors.append("缺 schema_version")
        if not payload.get("disclaimer"):
            errors.append("缺 disclaimer")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="只跑某题，如 P01")
    parser.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    args = parser.parse_args(argv)

    cases = json.loads(EVAL_PATH.read_text(encoding="utf-8")).get("cases") or []
    if args.case:
        cases = [case for case in cases if case["id"] == args.case]
        if not cases:
            print(f"没有找到用例 {args.case}", file=sys.stderr)
            return 2

    passed = 0
    failures: list[str] = []
    for case in cases:
        payload = compare(case["question"])
        errors = check(payload, case.get("expect") or {})
        if errors:
            failures.append(f"{case['id']}: " + "；".join(errors))
        else:
            passed += 1
        mark = "PASS" if not errors else "FAIL"
        print(f"[{mark}] {case['id']} {case.get('topic', '')}")
        for line in errors:
            print(f"       - {line}")

    summary = {"cases": len(cases), "passed": passed, "failed": len(cases) - passed, "failures": failures}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"跨期对比：{passed}/{len(cases)} 通过")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
