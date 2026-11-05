"""问答的控制台渲染：统一 CLI（``finkb ask``）与 ``python -m finkb.qa`` 共用。

只做输出格式化，不改变问答结果；JSON 模式返回完整 payload 供脚本消费。
"""

from __future__ import annotations

import json

from finkb.qa.audit_log import log as audit_log
from finkb.qa.pipeline import TIER_LABELS, ask
from finkb.qa.synthesizer import render
from finkb.qa.verifier import verify


def run(
    question: str,
    *,
    tier: str = "auto",
    explain: bool = False,
    as_json: bool = False,
    audit: bool = False,
) -> tuple[str, int]:
    """回答一个问题并返回（要打印的文本, 退出码）。"""
    payload = ask(question, tier=tier)
    if audit:
        audit_log({**payload, "question": question})
    rendered, used = render(payload.get("text", ""), payload.get("ledger", []))
    check = verify({**payload, "text": rendered})
    if as_json:
        body = {**payload, "text": rendered, "used_ledger": used, "verify": check}
        return json.dumps(body, ensure_ascii=False, indent=2), 0

    lines: list[str] = []
    name = payload.get("tier", "?")
    refused = "　（拒答）" if payload.get("refused") else ""
    lines.append(
        f"档位：{TIER_LABELS.get(name, name)}（{name}）"
        f"　置信度：{payload.get('confidence', 0):.2f}{refused}"
    )
    if explain:
        lines.extend("  " + json.dumps(step, ensure_ascii=False) for step in payload.get("trace", []))
    lines.append(f"答案：{rendered}")
    if payload.get("ledger"):
        lines.append("账本：")
        for entry in payload["ledger"]:
            suffix = f"  ← {entry.get('doc')}#P{entry.get('page')}" if entry.get("doc") else ""
            lines.append(
                f"  {entry['id']} = {entry['value']:.4f} {entry.get('unit', '')}"
                f"  [{entry.get('kind', 'fact')}]{suffix}"
            )
    if payload.get("citations"):
        lines.append("引用：" + " ".join(payload["citations"]))
    lines.append(f"校验：{'通过' if check['ok'] else '未通过 ' + str(check['issues'])}")
    return "\n".join(lines), 0
