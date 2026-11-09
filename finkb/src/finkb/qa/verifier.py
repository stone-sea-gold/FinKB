"""答案校验：数字绑定（ADR-001）与引用闸门。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from finkb.fact_engine.selector import FINKB_ROOT
from finkb.qa.synthesizer import format_value

PARSED_DIR = FINKB_ROOT / "data" / "parsed"
REF_RE = re.compile(r"\[Ref:\s*([^#\]]+)#P(\d+)\]")
NUMBER_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")
META_RE = re.compile(r'<!--\s*META:\s*(\{.*?\})\s*-->')


@lru_cache(maxsize=1)
def _page_texts() -> dict[tuple[str, int], str]:
    pages: dict[tuple[str, int], str] = {}
    if not PARSED_DIR.exists():
        return pages
    for doc_dir in PARSED_DIR.iterdir():
        file = doc_dir / "page_anchored.md"
        if not doc_dir.is_dir() or not file.exists():
            continue
        page, buffer = 0, []
        for line in file.read_text(encoding="utf-8").splitlines():
            match = META_RE.match(line.strip())
            if match:
                if page and buffer:
                    pages[(doc_dir.name, page)] = "\n".join(buffer)
                buffer = []
                try:
                    page = int(json.loads(match.group(1)).get("page") or 0)
                except json.JSONDecodeError:
                    page = 0
            else:
                buffer.append(line)
        if page and buffer:
            pages[(doc_dir.name, page)] = "\n".join(buffer)
    # 视觉解读页（图片/图表页通常没有文字层）：引用它们时用模型解读文本
    # 作为“页文本”参与存在性校验；数字仍只认账本或原文（见 ADR-075）。
    try:
        from finkb.search.vision_index import vision_texts

        for key, text in vision_texts(PARSED_DIR).items():
            pages.setdefault(key, text)
    except Exception:  # pragma: no cover - 检索层异常不应阻断校验
        pass
    return pages


@lru_cache(maxsize=1)
def _report_pages() -> dict[str, int]:
    out: dict[str, int] = {}
    if PARSED_DIR.exists():
        for doc_dir in PARSED_DIR.iterdir():
            report = doc_dir / "report.json"
            if report.exists():
                try:
                    out[doc_dir.name] = int(json.loads(report.read_text(encoding="utf-8")).get("pages") or 0)
                except (json.JSONDecodeError, ValueError):
                    pass
    return out


def verify(payload: dict) -> dict:
    """校验引用存在、页码在范围内、数字都能在账本/原文里找到。"""
    issues: list[str] = []
    text = payload.get("text") or ""
    ledger = payload.get("ledger") or []
    citations = payload.get("citations") or []
    if payload.get("refused"):
        return {"ok": True, "issues": [], "refs_checked": 0, "numbers_checked": 0}

    refs = REF_RE.findall(text + " " + " ".join(citations))
    page_texts = _page_texts()
    report_pages = _report_pages()
    for doc, page in refs:
        page = int(page)
        if doc not in report_pages:
            issues.append(f"引用文档不存在：{doc}")
            continue
        if page < 1 or page > report_pages[doc]:
            issues.append(f"引用页码越界：{doc}#P{page}（报告共 {report_pages[doc]} 页）")
            continue
        if (doc, page) not in page_texts:
            issues.append(f"引用页没有可检索文本：{doc}#P{page}")

    allowed = set()
    for entry in ledger:
        if "value" not in entry:
            continue
        value = float(entry["value"])
        allowed.add(format_value(value))
        allowed.add(f"{int(round(value))}")
        allowed.add(f"{value:g}")
        raw = entry.get("raw_value")
        if raw is not None:
            raw = float(raw)
            allowed.add(f"{raw:.2f}")
            allowed.add(f"{int(round(raw))}")
            allowed.add(f"{raw:g}")
    if payload.get("route") == "text":
        # 原文通道：数字是原文逐字摘录，不再做账本绑定；只校验引用页。
        return {"ok": not issues, "issues": issues, "refs_checked": len(refs), "numbers_checked": 0}
    # 原文通道：数字只要在引用页原文中逐字出现即可（原文引用，不是生成）
    cited_text = " ".join(page_texts.get((doc, int(page)), "") for doc, page in refs)
    stripped = REF_RE.sub("", text)
    stripped = re.sub(r"\b\d{6}_\d{4}_(?:年报|半年报|一季报|三季报)", "", stripped)  # 文档 id
    stripped = re.sub(r"\b\d{6}\b", "", stripped)      # 股票代码
    stripped = re.sub(r"P\d+", "", stripped)            # 页码
    stripped = re.sub(r"第\s*\d+\s*页", "", stripped)   # “第 32 页”
    numbers = [number.replace(",", "") for number in NUMBER_RE.findall(stripped)]
    unbound = []
    for number in numbers:
        if number in allowed:
            continue
        if re.fullmatch(r"(19|20)\d{2}", number):  # 年份
            continue
        if number in cited_text:  # 原文逐字引用
            continue
        unbound.append(number)
    if unbound:
        issues.append(f"未绑定数字：{sorted(set(unbound))}")
    return {
        "ok": not issues,
        "issues": issues,
        "refs_checked": len(refs),
        "numbers_checked": len(numbers),
    }
