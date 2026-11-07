"""多轮上下文验收（12 组宿主模拟，离线）：槽位继承/覆盖、补全问题、澄清。

模拟宿主的两种传参方式：
- ``previous_slots``：上一轮返回的结构化槽位（推荐）；
- ``previous_question``：只有上一轮原文，项目用规则解析兜底。

检查项：档位、是否拒答、inherited/overridden、resolved_question 关键词、
resolved_slots、clarify.missing、无 context 时行为不变。

用法：``.venv/bin/python eval/check_context.py [--case C01]``
退出码：0=12/12；1=有失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "finkb" / "src"))

from finkb.qa import pipeline  # noqa: E402

EVAL_PATH = ROOT / "eval" / "context_qa.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check(payload: dict, question: str, expect: dict) -> list[str]:
    errors: list[str] = []

    if "tier" in expect and payload.get("tier") != expect["tier"]:
        errors.append(f"tier={payload.get('tier')} 期望 {expect['tier']}")
    if "refused" in expect and bool(payload.get("refused")) != bool(expect["refused"]):
        errors.append(f"refused={payload.get('refused')} 期望 {expect['refused']}")
    if "inherited" in expect and sorted(payload.get("inherited") or []) != sorted(expect["inherited"]):
        errors.append(f"inherited={payload.get('inherited')} 期望 {expect['inherited']}")
    if "overridden" in expect and sorted(payload.get("overridden") or []) != sorted(expect["overridden"]):
        errors.append(f"overridden={payload.get('overridden')} 期望 {expect['overridden']}")

    resolved = payload.get("resolved_question") or ""
    for token in expect.get("resolved_contains") or []:
        if token not in resolved:
            errors.append(f"resolved_question 缺「{token}」：{resolved}")
    if expect.get("resolved_equals_question") and resolved != question:
        errors.append(f"resolved_question 被改写：{resolved}")

    text = payload.get("text") or ""
    for token in expect.get("text_contains") or []:
        if token not in text:
            errors.append(f"text 缺「{token}」：{text[:80]}")

    slots = payload.get("resolved_slots") or {}
    for key, value in (expect.get("slots") or {}).items():
        if slots.get(key) != value:
            errors.append(f"resolved_slots[{key}]={slots.get(key)!r} 期望 {value!r}")

    missing = payload.get("missing") or (payload.get("clarify") or {}).get("missing") or []
    if "clarify_missing" in expect and sorted(missing) != sorted(expect["clarify_missing"]):
        errors.append(f"missing={missing} 期望 {expect['clarify_missing']}")
    if "context_used" in expect and bool(payload.get("context_used")) != bool(expect["context_used"]):
        errors.append(f"context_used={payload.get('context_used')} 期望 {expect['context_used']}")
    return errors


def run_case(case: dict) -> tuple[int, int, list[str]]:
    turns = case["turns"]
    passed = 0
    failures: list[str] = []
    previous: dict | None = None
    previous_question: str | None = None
    for index, turn in enumerate(turns, start=1):
        expect = turn.get("expect") or {}
        context = None
        if previous is not None:
            if expect.get("context_mode") == "previous_question":
                context = {"previous_question": previous_question}
            else:
                context = {
                    "previous_question": previous_question,
                    "previous_slots": previous.get("resolved_slots") or {},
                }
        payload = pipeline.ask(turn["question"], synthesize=False, context=context)
        errors = check(payload, turn["question"], expect)
        if errors:
            failures.append(f"{case['id']} turn{index}: " + "；".join(errors))
        else:
            passed += 1
        previous = payload
        previous_question = turn["question"]
    return passed, len(turns), failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="只跑某组，如 C01")
    parser.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    args = parser.parse_args(argv)

    data = load(EVAL_PATH)
    cases = data.get("cases") or []
    if args.case:
        cases = [case for case in cases if case["id"] == args.case]
        if not cases:
            print(f"没有找到用例 {args.case}", file=sys.stderr)
            return 2

    total_passed = total_turns = 0
    all_failures: list[str] = []
    for case in cases:
        passed, count, failures = run_case(case)
        total_passed += passed
        total_turns += count
        all_failures.extend(failures)
        mark = "PASS" if passed == count else "FAIL"
        print(f"[{mark}] {case['id']} {case.get('topic', '')} ({passed}/{count})")
        for line in failures:
            print(f"       - {line}")

    summary = {
        "cases": len(cases),
        "turns": total_turns,
        "passed": total_passed,
        "failed": total_turns - total_passed,
        "failures": all_failures,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"多轮上下文：{total_passed}/{total_turns} 轮通过（{len(cases)} 组）")
    return 0 if total_passed == total_turns else 1


if __name__ == "__main__":
    raise SystemExit(main())
