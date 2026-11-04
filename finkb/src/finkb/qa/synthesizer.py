"""答案合成：把 ``{{cN}}`` 占位符替换成账本里的数字（ADR-001 数字闸门）。"""

from __future__ import annotations

import re

PLACEHOLDER_RE = re.compile(r"\{\{c(\d+)\}\}")


def format_value(value: float) -> str:
    """统一两位小数；负号保留。"""
    return f"{value:.2f}"


def render(text: str, ledger: list[dict]) -> tuple[str, list[str]]:
    """渲染占位符，返回 (答案文本, 用到的账本 id)。"""
    by_id = {str(entry["id"]): entry for entry in ledger}

    def replace(match: re.Match) -> str:
        key = f"c{match.group(1)}"
        entry = by_id.get(key)
        if not entry:
            return match.group(0)
        value = float(entry["value"])
        return f"{int(round(value))}" if entry.get("format") == "int" else format_value(value)

    return PLACEHOLDER_RE.sub(replace, text), [f"c{m.group(1)}" for m in PLACEHOLDER_RE.finditer(text)]
