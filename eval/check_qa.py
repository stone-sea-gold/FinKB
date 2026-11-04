"""50 题问答评测：数字 15 + 文字 20 + 推理 10 + 负例 5。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "finkb" / "src"))

from finkb.qa.answer import answer  # noqa: E402

REF_RE = re.compile(r"\[Ref:\s*([^#\]]+)#P(\d+)\]")


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def citation_pages(payload: dict) -> set[tuple[str, int]]:
    return {(doc, int(page)) for doc, page in REF_RE.findall(payload.get("text", "") + " " + " ".join(payload.get("citations", [])))}


def check_numeric(payload: dict, gold: dict) -> tuple[bool, str]:
    expected = float(gold["answer_exact"])
    if payload.get("refused"):
        return False, "拒答了"
    value = payload.get("value")
    if value is None:
        return False, "没有 value"
    ok = abs(float(value) - expected) <= max(1e-6, abs(expected) * 1e-6)
    return ok, f"{float(value):.6f} vs {expected:.6f}"


def check_text(payload: dict, gold: dict) -> tuple[bool, str]:
    if payload.get("route") != "text" or payload.get("refused"):
        return False, f"route={payload.get('route')}"
    expected = {(item["doc_id"], int(page)) for item in gold["expected"] for page in item["pages"]}
    hit = citation_pages(payload) & expected
    return bool(hit), f"命中 {len(hit)} 页" if hit else "Top 引用未命中期望页"


def check_reasoning(payload: dict, gold: dict) -> tuple[bool, str]:
    if payload.get("refused"):
        return False, "拒答了"
    kind = gold["type"]
    data = payload.get("data") or {}
    if kind == "class":
        got = {member.get("company") for member in data.get("members", [])}
        expected = set(gold["expected_companies"])
        return got == expected, f"{sorted(got)} vs {sorted(expected)}"
    if kind == "relation":
        got = {edge.get("dst") for edge in data.get("edges", [])}
        missing = [name for name in gold["expected_contains"] if name not in got]
        return not missing, f"缺 {missing}" if missing else "包含期望实体"
    if kind == "note":
        pages = {page for _, page in citation_pages(payload)}
        ok = bool(pages & set(gold["expected_pages"]))
        return ok, f"引用页 {sorted(pages)} vs {gold['expected_pages']}"
    return False, f"未知题型 {kind}"


def check_negative(payload: dict, gold: dict) -> tuple[bool, str]:
    return bool(payload.get("refused")), payload.get("text", "")[:40]


def main() -> None:
    numeric = load("eval/gold_qa.json")["questions"]
    text = load("eval/text_qa.json")["questions"]
    reasoning = load("eval/gold_reasoning.json")["questions"]
    negative = load("eval/gold_negative.json")["questions"]
    results = []
    for gold in numeric:
        payload = answer(gold["question"])
        ok, note = check_numeric(payload, gold)
        results.append(("numeric", gold["id"], ok, note))
    for gold in text:
        payload = answer(gold["question"])
        ok, note = check_text(payload, gold)
        results.append(("text", gold["id"], ok, note))
    for gold in reasoning:
        payload = answer(gold["question"])
        ok, note = check_reasoning(payload, gold)
        results.append(("reasoning", gold["id"], ok, note))
    for gold in negative:
        payload = answer(gold["question"])
        ok, note = check_negative(payload, gold)
        results.append(("negative", gold["id"], ok, note))

    summary = {}
    for kind, qid, ok, note in results:
        bucket = summary.setdefault(kind, {"passed": 0, "total": 0, "fails": []})
        bucket["total"] += 1
        bucket["passed"] += int(ok)
        if not ok:
            bucket["fails"].append(f"{qid}: {note}")
    total = len(results)
    passed = sum(int(ok) for _, _, ok, _ in results)
    print(json.dumps({"total": total, "passed": passed, "by_type": summary,
                      "fails": [f"{kind} {qid}: {note}" for kind, qid, ok, note in results if not ok]},
                     ensure_ascii=False, indent=2))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
