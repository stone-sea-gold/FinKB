"""合成子系统评测（15 题，真实 LLM）：合成启用率、校验通过率、引用覆盖、拒答。

与 ``check_qa.py`` 的区别：
- 走 ``pipeline.ask(..., synthesize=backend)``，测的是“模式 A：项目内受约束生成”；
- 需要真实模型（``FINKB_LLM_SYNTH=1`` + key），**不进** ``scripts/check.sh`` 的离线门禁；
- 负例只要求拒答且不调模型；正例要求合成启用、V1–V7 全过、答案带引用。

用法：``.venv/bin/python eval/check_synth.py [--out data/_eval/synth_results.json]``
退出码：0=15/15；1=有失败；2=未配置模型（跳过）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "finkb" / "src"))

from finkb.qa import pipeline  # noqa: E402
from finkb.qa.synth import backend_from_env  # noqa: E402

REF_RE = re.compile(r"\[Ref:\s*[^#\]]+#P\d+\]")
EVAL_PATH = ROOT / "eval" / "synth_qa.json"
DEFAULT_OUT = ROOT / "finkb" / "data" / "_eval" / "synth_results.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check(payload: dict, gold: dict) -> tuple[bool, str]:
    expect = gold["expect"]
    synthesis = payload.get("synthesis") or {}
    used = bool(synthesis.get("used"))
    refused = bool(payload.get("refused"))

    if not expect.get("synthesis"):
        if not refused:
            return False, f"应拒答但回答了（tier={payload.get('tier')}）"
        if used:
            return False, "负例不应调用合成"
        return True, f"拒答（{payload.get('tier')}）"

    if refused:
        return False, "正例被拒答"
    if not used:
        return False, f"合成未启用：{synthesis.get('reason', '?')} {synthesis.get('errors', [])[:2]}"
    checks = synthesis.get("checks") or {}
    if not checks or not all(checks.values()):
        return False, f"校验未全过：{checks}"
    text = payload.get("text") or ""
    refs = REF_RE.findall(text)
    if not refs and not payload.get("citations"):
        return False, "答案没有引用"
    if len(text) < 20:
        return False, f"答案过短：{text!r}"
    return True, f"合成 {len(text)} 字 / {len(refs)} 处引用 / 尝试 {synthesis.get('attempts')} 次"


def main() -> None:
    parser = argparse.ArgumentParser(description="合成子系统 15 题评测（真实 LLM）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="结果明细写入路径")
    args = parser.parse_args()

    backend = backend_from_env()
    if backend is None:
        print(json.dumps({"skipped": True, "reason": "未配置 FINKB_LLM_SYNTH=1 / FINKB_LLM_SYNTH_API_KEY"},
                         ensure_ascii=False, indent=2))
        raise SystemExit(2)

    golds = load(EVAL_PATH)["questions"]
    results = []
    for gold in golds:
        payload = pipeline.ask(gold["question"], tier=gold.get("tier", "auto"), synthesize=backend)
        ok, note = check(payload, gold)
        results.append(
            {
                "id": gold["id"],
                "question": gold["question"],
                "tier": payload.get("tier"),
                "ok": ok,
                "note": note,
                "text": payload.get("text"),
                "citations": payload.get("citations", []),
                "synthesis": payload.get("synthesis"),
            }
        )

    passed = sum(int(item["ok"]) for item in results)
    used = [item for item in results if (item["synthesis"] or {}).get("used")]
    attempts = [(item["synthesis"] or {}).get("attempts", 0) for item in used]
    summary = {
        "total": len(results),
        "passed": passed,
        "synthesis_used": len(used),
        "refused": sum(int(bool(item.get("synthesis") is None and item["note"].startswith("拒答"))) for item in results),
        "avg_attempts": round(sum(attempts) / len(attempts), 2) if attempts else 0,
        "avg_chars": round(sum(len(item["text"] or "") for item in used) / len(used)) if used else 0,
        "fails": [f"{item['id']}: {item['note']}" for item in results if not item["ok"]],
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("total", "passed", "synthesis_used", "refused",
                                                   "avg_attempts", "avg_chars", "fails")},
                     ensure_ascii=False, indent=2))
    print(f"明细：{out}")
    raise SystemExit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
