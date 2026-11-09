"""解析质量体检（ADR-071）：把 MinerU 的“静默丢页”变成显式报告。

MinerU 的几种缺内容形态要分开看，不能混成“丢表/丢页”：

- ``empty_tables``：``table_body``/``html`` 为空的表格块。多数是**跨页长表的续页
  占位**（整表 body 放在起始页的块里，后续页只留空块），数据并没有丢；只有不属于
  任何跨页长表的空块才是可疑的。传入 ``tables``（物理表，含 page_start/page_end）
  时按页覆盖关系自动区分 ``placeholder`` 与 ``suspicious``。
- ``chart_pages`` / ``chart_blocks``：含图表块的页。chart 块本身无结构文字；
  若 PDF 有文字层，``content_list_to_pages`` 的文字层兜底可能抽出零散标签/数字，
  但顺序与归属不可靠，不作为事实来源。
- ``image_only_pages`` / ``no_content_pages``：整页只有图片块（封面/插图/扫描图）。
  这类页没有文字层，解析时被直接跳过；这里显式标记为“无内容”，不参与检索与引用。
- ``missing_pages``：``content_list`` 完全没有覆盖到的 PDF 页（与 PDF 总页数比）。

调用方（``cli.py parse``）把结果写进 ``report.json`` 的 ``quality`` 字段并打印
告警；是否恢复（二次 OCR/人工补录）由人决定。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

_NOISE_TYPES = {"header", "footer", "page_number", "page_footnote"}
_TEXT_TYPES = {"text", "table", "chart", "equation", "list", "interline_equation"}


def _table_body(item: dict[str, Any]) -> str:
    return str(item.get("table_body") or item.get("html") or "").strip()


def _table_span(table: Any) -> tuple[int, int] | None:
    """从物理表（对象或 report.json 的 dict）取页码范围。"""
    if isinstance(table, dict):
        start, end = table.get("page_start"), table.get("page_end")
    else:
        start, end = getattr(table, "page_start", None), getattr(table, "page_end", None)
    if isinstance(start, int) and isinstance(end, int):
        return start, end
    return None


def analyze_quality(
    content_list: list[dict[str, Any]],
    *,
    pdf_path: str | Path | None = None,
    pdf_pages: int | None = None,
    tables: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """统计 ``content_list`` 的缺表/缺字/缺页情况。

    ``tables`` 是物理表列表（``page_start``/``page_end``）；给了才能把空表块区分为
    “续页占位”和“可疑空表”。``pdf_pages`` 只在 ``pdf_path`` 可读时给出；
    ``missing_pages`` 按 PDF 总页数计算，没有 PDF 时为空。
    """
    by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in content_list:
        page_idx = item.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        by_page[page_idx].append(item)

    spans = [span for table in (tables or []) if (span := _table_span(table)) is not None]

    def _is_placeholder(page: int) -> bool:
        # 续页占位：本页被一张从更早页开始的长表覆盖（page_start < page <= page_end）
        return any(start < page <= end for start, end in spans)

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
                    page = page_idx + 1
                    empty_tables.append(
                        {
                            "page": page,
                            "table_index": table_seq,
                            "caption": str(
                                item.get("table_caption") or item.get("caption") or ""
                            )[:120],
                            "placeholder": _is_placeholder(page),
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

    placeholder = [item for item in empty_tables if item["placeholder"]]
    suspicious = [item for item in empty_tables if not item["placeholder"]]
    return {
        "pdf_pages": pdf_pages,
        "parsed_pages": len(by_page),
        "empty_tables": empty_tables,
        "empty_table_count": len(empty_tables),
        "placeholder_table_count": len(placeholder),
        "suspicious_empty_tables": suspicious,
        "suspicious_empty_table_count": len(suspicious),
        "chart_pages": sorted(chart_pages),
        "chart_blocks": chart_blocks,
        "image_only_pages": sorted(image_only_pages),
        "no_content_pages": sorted(image_only_pages),
        "missing_pages": missing_pages,
    }


def quality_warnings(quality: dict[str, Any]) -> list[str]:
    """把统计结果翻成给用户看的中文告警（只报真正需要人处理/知情的项）。"""
    messages: list[str] = []
    suspicious = quality.get("suspicious_empty_tables")
    if suspicious is None:
        # 兼容旧 report.json：没有分类字段时按全部空表处理
        suspicious = quality.get("empty_tables") or []
    if suspicious:
        pages = sorted({item["page"] for item in suspicious})
        messages.append(
            f"可疑空表 {len(suspicious)} 张（页 {', '.join(f'P{p}' for p in pages[:12])}"
            + ("…" if len(pages) > 12 else "")
            + "）：这些空表块不属于任何跨页长表，可能真的丢了内容，建议核对原文。"
        )
    chart_pages = quality.get("chart_pages") or []
    if chart_pages:
        messages.append(
            f"图表页 {len(chart_pages)} 页（{', '.join(f'P{p}' for p in chart_pages[:8])}"
            + ("…" if len(chart_pages) > 8 else "")
            + "）：chart 块无结构文字，文字层兜底只能抽到零散标签/数字，不作为事实来源。"
        )
    image_only = quality.get("no_content_pages")
    if image_only is None:
        image_only = quality.get("image_only_pages") or []
    if image_only:
        messages.append(
            f"纯图片页 {len(image_only)} 页（{', '.join(f'P{p}' for p in image_only[:8])}"
            + ("…" if len(image_only) > 8 else "")
            + "）：无文字层，已标记为无内容（封面/插图），不参与检索与引用。"
        )
    missing = quality.get("missing_pages") or []
    if missing:
        messages.append(
            f"未覆盖页 {len(missing)} 页（{', '.join(f'P{p}' for p in missing[:8])}"
            + ("…" if len(missing) > 8 else "")
            + "）：content_list 里没有这些页。"
        )
    return messages
