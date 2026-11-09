"""视觉解读页的轻量检索（``source=vision``，不进事实层）。

``finkb vision`` 把图片/图表页的模型解读写进
``data/parsed/<doc_id>/vision_pages.json``。本模块把这些注释加载成可检索的
页面片段：只保留 chart / infographic / other（封面、封底、装饰、空白页跳过），
用关键词打分，不依赖 embedding，也不改写 chunks.db。

设计边界（ADR-075）：
- 视觉片段只作为文档问答的补充证据，引用时标“视觉模型解读，建议核对原文”；
- 不写入 ``page_anchored.md``、不进事实层、不参与数字绑定；
- 过滤条件与文本检索一致：公司代码 / 期间。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jieba

from finkb.common import paths

# 只有这些类型值得进入检索；cover/back_cover/decorative/blank/error 一律跳过。
VALUABLE_KINDS = ("chart", "infographic", "other")

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z]+|\d+(?:\.\d+)?%?")
_STOPWORDS = {
    "公司", "年报", "报告", "集团", "股份", "有限", "我们", "以及", "其中", "根据",
    "这个", "那个", "如何", "什么", "哪些", "情况", "相关", "方面", "主要", "年度",
}
_DOC_ID_RE = re.compile(r"^(\d{6})_([0-9]{4}(?:H1|Q1-Q3|Q1|Q3)?)_(.+)$")


def _parsed_root(parsed_root: str | Path | None) -> Path:
    return Path(parsed_root) if parsed_root else paths.parsed_dir()


def _doc_key(doc_id: str) -> tuple[str, str]:
    match = _DOC_ID_RE.match(doc_id)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def _tables_text(tables: list[dict]) -> str:
    parts: list[str] = []
    for table in tables or []:
        caption = str(table.get("caption") or "").strip()
        columns = [str(col) for col in (table.get("columns") or [])]
        rows = table.get("rows") or []
        if caption:
            parts.append(caption)
        if columns:
            parts.append(" | ".join(columns))
        for row in rows:
            parts.append(" | ".join(str(cell) for cell in row))
    return "\n".join(parts)


def _note_text(note: dict) -> str:
    description = str(note.get("description") or "").strip()
    tables = _tables_text(note.get("tables") or [])
    return "\n".join(part for part in (description, tables) if part)


def load_notes(parsed_root: str | Path | None = None) -> list[dict[str, Any]]:
    """读取全部 ``vision_pages.json``，返回可检索的注释（按 doc_id/page 排序）。"""
    root = _parsed_root(parsed_root)
    out: list[dict[str, Any]] = []
    if not root.exists():
        return out
    for doc_dir in sorted(root.iterdir()):
        file = doc_dir / "vision_pages.json"
        if not doc_dir.is_dir() or not file.exists():
            continue
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        company, period = _doc_key(doc_dir.name)
        for page in payload.get("pages") or []:
            if page.get("error"):
                continue
            kind = str(page.get("kind") or "")
            if kind not in VALUABLE_KINDS:
                continue
            note = {
                "doc_id": doc_dir.name,
                "company": company,
                "period": period,
                "page": int(page.get("page") or 0),
                "kind": kind,
                "description": str(page.get("description") or "").strip(),
                "tables": page.get("tables") or [],
            }
            note["text"] = _note_text(note)
            if note["text"]:
                out.append(note)
    return out


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for piece in jieba.lcut(text or ""):
        piece = piece.strip()
        if not piece or piece in _STOPWORDS:
            continue
        for token in _TOKEN_RE.findall(piece):
            if token in _STOPWORDS or (len(token) == 1 and not token.isdigit()):
                continue
            tokens.append(token.lower())
    return tokens


def _match_filters(note: dict, filters: dict | None) -> bool:
    if not filters:
        return True
    company = str(filters.get("company") or "")
    period = str(filters.get("period") or "")
    if company and note["company"] != company:
        return False
    if period and note["period"] != period:
        return False
    return True


def search(
    query: str,
    *,
    filters: dict | None = None,
    limit: int = 3,
    parsed_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """按关键词给视觉注释打分，返回命中的注释（score>0，最多 limit 条）。"""
    tokens = _tokens(query)
    if not tokens:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for note in load_notes(parsed_root):
        if not _match_filters(note, filters):
            continue
        description = note["description"].lower()
        body = note["text"].lower()
        score = 0.0
        for token in tokens:
            if token in description:
                score += 3.0
            elif token in body:
                score += 1.0
        if score > 0:
            item = dict(note)
            item["score"] = score
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["doc_id"], pair[1]["page"]))
    return [item for _, item in scored[:limit]]


def to_evidence(note: dict[str, Any]) -> dict[str, Any]:
    """把视觉注释转成 ``answer_text`` 的证据条目（与 hybrid 结果同构）。"""
    text = str(note.get("text") or "")
    description = str(note.get("description") or "").strip()
    snippet = f"【视觉解读】{description or text[:200]}"
    if len(snippet) > 320:
        snippet = snippet[:319] + "…"
    return {
        "chunk_id": f"vision:{note['doc_id']}#P{note['page']}",
        "doc_id": note["doc_id"],
        "page_num": int(note["page"]),
        "section": "视觉解读",
        "snippet": snippet,
        "text": text[:1500],
        "block_type": "vision",
        "vision": True,
    }


def vision_texts(parsed_root: str | Path | None = None) -> dict[tuple[str, int], str]:
    """引用校验用：``(doc_id, page) -> 视觉解读文本``（带明确前缀）。"""
    out: dict[tuple[str, int], str] = {}
    for note in load_notes(parsed_root):
        out[(note["doc_id"], int(note["page"]))] = "【视觉模型解读】\n" + note["text"]
    return out
