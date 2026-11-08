"""解析质量体检（ADR-071）：把 MinerU 的“静默丢页”变成显式报告。

MinerU 对部分宽表会返回空 ``table_body``、对 ``chart`` 块不返回文字、对纯图片页
没有文字层。过去这些页在解析产物里直接消失，``report.json`` 只记录“解析出了
多少”，不记录“少了什么”。本模块只做统计，不尝试恢复：

- ``empty_tables``：``table_body``/``html`` 为空的表格块（页码 + 序号）；
- ``chart_pages`` / ``chart_blocks``：含图表块的页；
- ``image_only_pages``：整页只有图片块（通常无文字层，无法用文字兜底）；
- ``missing_pages``：``content_list`` 完全没有覆盖到的 PDF 页（与 PDF 总页数比）。

调用方（``cli.py parse``）把结果写进 ``report.json`` 的 ``quality`` 字段并打印
告警；是否恢复（二次 OCR/人工补录）由人决定。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

_NOISE_TYPES = {"header", "footer", "page_number", "page_footnote"}
_TEXT_TYPES = {"text", "table", "chart", "equation", "list", "interline_equation"}


def _table_body(item: dict[str, Any]) -> str:
    return str(item.get("table_body") or item.get("html") or "").strip()


def analyze_quality(
    content_list: list[dict[str, Any]],
    *,
    pdf_path: str | Path | None = None,
    pdf_pages: int | None = None,
) -> dict[str, Any]:
    """统计 ``content_list`` 的缺表/缺字/缺页情况。

    ``pdf_pages`` 只在 ``pdf_path`` 可读时给出；``missing_pages`` 按 PDF 总页数
    计算，没有 PDF 时为空（无法判断“没被覆盖”的页）。
    """
    by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in content_list:
        page_idx = item.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        by_page[page_idx].append(item)

    empty_tables: list[dict[str, Any]] = []
    chart_blocks = 0
    chart_pages: set[int] = set()
    for page_idx, items in by_page.items():
        table_seq = 0
        for item in items:
            kind = str(item.get("type") or "")
            if kind == "table":
                table_seq += 1
                if not _table_body(item):
                    empty_tables.append(
                        {
                            "page": page_idx + 1,
                            "table_index": table_seq,
                            "caption": str(item.get("table_caption") or item.get("caption") or "")[:120],
                        }
                    )
            elif kind == "chart":
                chart_blocks += 1
                chart_pages.add(page_idx + 1)

    image_only_pages: list[int] = []
    for page_idx, items in by_page.items():
        kinds = {str(item.get("type") or "") for item in items if item.get("type")}
        if kinds and kinds <= ({"image"} | _NOISE_TYPES):
            image_only_pages.append(page_idx + 1)

    missing_pages: list[int] = []
    if pdf_pages is None and pdf_path:
        try:
            from pypdf import PdfReader

            pdf_pages = len(PdfReader(str(pdf_path)).pages)
        except Exception:  # pragma: no cover - 损坏 PDF / 缺依赖时不影响解析
            pdf_pages = None
    if pdf_pages:
        covered = {page_idx + 1 for page_idx in by_page}
        missing_pages = [page for page in range(1, pdf_pages + 1) if page not in covered]

    return {
        "pdf_pages": pdf_pages,
        "parsed_pages": len(by_page),
        "empty_tables": empty_tables,
        "empty_table_count": len(empty_tables),
        "chart_pages": sorted(chart_pages),
        "chart_blocks": chart_blocks,
        "image_only_pages": sorted(image_only_pages),
        "missing_pages": missing_pages,
    }


def quality_warnings(quality: dict[str, Any]) -> list[str]:
    """把统计结果翻成给用户看的中文告警（不重复报同页）。"""
    messages: list[str] = []
    empty = quality.get("empty_tables") or []
    if empty:
        pages = sorted({item["page"] for item in empty})
        messages.append(
            f"空表 {len(empty)} 张（页 {', '.join(f'P{p}' for p in pages[:12])}"
            + ("…" if len(pages) > 12 else "")
            + "）：MinerU 未返回表格内容，这些表没有进入下游。"
        )
    chart_pages = quality.get("chart_pages") or []
    if chart_pages:
        messages.append(
            f"图表页 {len(chart_pages)} 页（{', '.join(f'P{p}' for p in chart_pages[:8])}"
            + ("…" if len(chart_pages) > 8 else "")
            + "）：chart 块无文字，图表数据未进入知识库。"
        )
    image_only = quality.get("image_only_pages") or []
    if image_only:
        messages.append(
            f"纯图片页 {len(image_only)} 页（{', '.join(f'P{p}' for p in image_only[:8])}"
            + ("…" if len(image_only) > 8 else "")
            + "）：无文字层，无法自动恢复。"
        )
    missing = quality.get("missing_pages") or []
    if missing:
        messages.append(
            f"未覆盖页 {len(missing)} 页（{', '.join(f'P{p}' for p in missing[:8])}"
            + ("…" if len(missing) > 8 else "")
            + "）：content_list 里没有这些页。"
        )
    return messages
