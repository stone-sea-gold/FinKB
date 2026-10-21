"""页码锚定 Markdown 解析与表格识别的单元测试。"""

from __future__ import annotations

import json

from finkb.parser.markdown_blocks import (
    html_table_to_markdown,
    is_separator_row,
    parse_markdown_table,
    parse_page_anchored_markdown,
    split_markdown_row,
)
from finkb.common.models import DocMeta

DOC = "002594_2025_年报"


def meta(page: int, section: str | None = None) -> str:
    payload = {"doc": DOC, "page": page}
    if section:
        payload["section"] = section
    return f"<!-- META: {json.dumps(payload, ensure_ascii=False)} -->"


# --------------------------------------------------------------------------- #
# 行级解析
# --------------------------------------------------------------------------- #
def test_split_row_handles_escaped_pipe() -> None:
    assert split_markdown_row(r"| a | b\|c | d |") == ["a", "b|c", "d"]


def test_split_row_accepts_missing_edge_pipes() -> None:
    assert split_markdown_row("a | b") == ["a", "b"]


def test_is_separator_row_detects_alignment_variants() -> None:
    assert is_separator_row("| --- | --- |")
    assert is_separator_row("|:---|---:|")
    assert is_separator_row("| :-: | --- |")
    assert not is_separator_row("| 项目 | 2025年 |")
    assert not is_separator_row("普通文本")


# --------------------------------------------------------------------------- #
# 表格解析
# --------------------------------------------------------------------------- #
def test_table_with_separator_has_header() -> None:
    table = parse_markdown_table(
        ["| 项目 | 2025年 |", "| --- | --- |", "| 营业收入 | 1 |"],
        DocMeta(doc_id=DOC, page=42),
    )
    assert table.header == ["项目", "2025年"]
    assert table.rows == [["营业收入", "1"]]
    assert table.has_separator
    assert table.ncols == 2


def test_headerless_table_treats_first_row_as_data() -> None:
    table = parse_markdown_table(
        ["| 净利润 | 4,000,000 |", "| 归母净利润 | 3,900,000 |"],
        DocMeta(doc_id=DOC, page=43),
    )
    assert table.header is None
    assert table.is_headerless
    assert len(table.rows) == 2
    assert table.ncols == 2


def test_headerless_table_with_separator_only() -> None:
    """有分隔行但没有真正的表头时，首行仍按表头处理。"""
    table = parse_markdown_table(
        ["| 净利润 | 4,000,000 |", "| --- | --- |"],
        DocMeta(doc_id=DOC, page=43),
    )
    assert table.header == ["净利润", "4,000,000"]
    assert table.rows == []


# --------------------------------------------------------------------------- #
# 文档级解析
# --------------------------------------------------------------------------- #
def test_pages_are_grouped_and_sorted() -> None:
    markdown = f"""{meta(43, "第三节")}
第二页内容

{meta(42, "第一节")}
第一页内容
"""
    pages = parse_page_anchored_markdown(markdown, default_doc=DOC)
    assert [page.number for page in pages] == [42, 43]
    assert pages[0].blocks[0].meta.section == "第一节"


def test_block_kinds_are_classified() -> None:
    markdown = f"""{meta(42)}
## 第三节 管理层讨论与分析

{meta(42)}
公司经营情况如下。

{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |
"""
    pages = parse_page_anchored_markdown(markdown, default_doc=DOC)
    assert [b.kind for b in pages[0].blocks] == ["heading", "text", "table"]
    assert pages[0].blocks[2].table is not None


def test_markdown_without_meta_falls_back_to_page_one() -> None:
    pages = parse_page_anchored_markdown("纯文本，没有任何 META 注释。", default_doc=DOC)
    assert len(pages) == 1
    assert pages[0].number == 1
    assert pages[0].blocks[0].meta.doc_id == DOC


def test_broken_meta_json_does_not_crash() -> None:
    markdown = "<!-- META: {broken json} -->\n正文"
    pages = parse_page_anchored_markdown(markdown, default_doc=DOC)
    assert len(pages) == 1
    assert pages[0].blocks[0].meta.doc_id == DOC


def test_page_must_be_one_based() -> None:
    import pytest

    with pytest.raises(ValueError):
        DocMeta(doc_id=DOC, page=0)


# --------------------------------------------------------------------------- #
# HTML 表格转换（MinerU 的 table_body）
# --------------------------------------------------------------------------- #
def test_html_table_converts_to_pipe_table() -> None:
    html = (
        "<table><tr><th>项目</th><th>2025年</th></tr>"
        "<tr><td>营业收入</td><td>77,201,000</td></tr></table>"
    )
    markdown = html_table_to_markdown(html)
    lines = markdown.splitlines()
    assert lines[0] == "| 项目 | 2025年 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 营业收入 | 77,201,000 |"


def test_html_table_handles_ragged_rows() -> None:
    html = "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>"
    lines = html_table_to_markdown(html).splitlines()
    assert lines[2] == "| c |  |"


def test_html_table_empty_returns_empty_string() -> None:
    assert html_table_to_markdown("<p>no table</p>") == ""
