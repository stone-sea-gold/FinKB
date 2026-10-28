"""页码锚定 Markdown 解析与表格识别的单元测试。"""

from __future__ import annotations

import json

from finkb.parser.markdown_blocks import (
    html_table_layout,
    html_table_to_markdown,
    html_table_to_rows,
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


def test_rowspan_is_expanded_into_following_rows() -> None:
    """``rowspan`` 进位格必须补进后续行，否则第二层表头整体左移一列。

    实测 P13「快报产量/快报销量」：首格 ``rowspan="2"`` 占住第 0 列，
    第 1 行只有 6 个单元格，若按顺序摆放就会从第 0 列开始，与上层的
    ``快报产量``／``快报销量`` 分组错位。
    """
    html = (
        '<table><tr><td rowspan="2"></td><td colspan="3">快报产量</td>'
        '<td colspan="3">快报销量</td></tr>'
        "<tr><td>本报告期</td><td>上年同期</td><td>与上年同比增减</td>"
        "<td>本报告期</td><td>上年同期</td><td>与上年同比增减</td></tr>"
        "<tr><td>-乘用车</td><td>4,479,392</td><td>4,281,084</td><td>4.63%</td>"
        "<td>4,545,423</td><td>4,250,370</td><td>6.94%</td></tr></table>"
    )
    layout = html_table_layout(html)

    assert layout.header_layers == 2
    assert layout.header_rows == [
        ["", "快报产量", "", "", "快报销量", "", ""],
        [
            "",
            "本报告期",
            "上年同期",
            "与上年同比增减",
            "本报告期",
            "上年同期",
            "与上年同比增减",
        ],
    ]
    # 压平后每个列名都要带上它所属的分组，否则「本报告期」在两组里无法区分。
    assert layout.header == [
        "",
        "快报产量/本报告期",
        "快报产量/上年同期",
        "快报产量/与上年同比增减",
        "快报销量/本报告期",
        "快报销量/上年同期",
        "快报销量/与上年同比增减",
    ]
    assert layout.rows[0][0] == "-乘用车"
    assert layout.rows[0][1] == "4,479,392"
    assert len({len(row) for row in layout.rows}) == 1


def test_rowspan_placeholder_row_without_own_cells_is_dropped() -> None:
    """自身一格内容都没有的 ``<tr>`` 是 MinerU 给 rowspan 补的占位行，必须剔除。"""
    html = (
        '<table><tr><td rowspan="2">A</td><td rowspan="2">B</td></tr>'
        "<tr><td></td></tr>"
        "<tr><td>1</td><td>2</td></tr></table>"
    )
    rows, _, _ = html_table_to_rows(html)

    assert rows == [["A", "B"], ["1", "2"]]


def test_ragged_group_row_with_empty_parent_cell_is_layer_two() -> None:
    """上一层某列空着（没有分组名）时，这一层给该列取名不阻止多行判定。

    实测来源：P180「合营企业」表，MinerU 把 `本年变动` 的 colspan 少算了两列，
    在行尾补了两个空 `<td>`，末两列的分组单元格就丢了。若要求「下一层所有自有列
    都必须落在分组区间内」，这张表会退化成单层表头，`本年变动/追加投资` 的分组
    信息整层丢失。
    """
    html = (
        "<table>"
        '<tr><td rowspan="2">合营企业</td><td colspan="2">本年变动</td><td></td></tr>'
        "<tr><td>追加投资</td><td>其他</td><td>年末余额</td></tr>"
        "<tr><td>比亚迪汽车金融</td><td>1</td><td>2</td><td>3</td></tr>"
        "</table>"
    )

    layout = html_table_layout(html)

    assert len(layout.header_rows) == 2
    assert layout.header == ["合营企业", "本年变动/追加投资", "本年变动/其他", "年末余额"]
    assert layout.rows == [["比亚迪汽车金融", "1", "2", "3"]]


# --------------------------------------------------------------------------- #
# 首格为空的表头（MinerU 输出里整行表头的第 0 格是空 <td>）
# --------------------------------------------------------------------------- #
def test_header_with_empty_first_cell_is_recognized() -> None:
    """P150 折旧年限表：首行 ``['', '估计使用年限', ...]``，第 0 格为空。"""
    html = (
        "<table><tr><td></td><td>估计使用年限</td><td>预计净残值率</td><td>年折旧率</td></tr>"
        "<tr><td>房屋及建筑物</td><td>3-70年</td><td>0%-5%</td><td>1.4%-33.3%</td></tr>"
        "<tr><td>机器设备</td><td>3-12年</td><td>0%-5%</td><td>7.9%-33.3%</td></tr></table>"
    )
    layout = html_table_layout(html)

    assert layout.header == ["", "估计使用年限", "预计净残值率", "年折旧率"]
    assert layout.rows[0][0] == "房屋及建筑物"


def test_empty_first_cell_two_column_header_is_recognized() -> None:
    """P248：两列表的列名在第二格，首格空。"""
    html = (
        "<table><tr><td></td><td>以权益结算的股份支付费用</td></tr>"
        "<tr><td>研发人员</td><td>395,229</td></tr>"
        "<tr><td>管理人员</td><td>248,378</td></tr></table>"
    )
    layout = html_table_layout(html)

    assert layout.header == ["", "以权益结算的股份支付费用"]
    assert len(layout.rows) == 2


def test_year_header_with_empty_first_cell_is_recognized() -> None:
    """赛力斯 P10：被吞的表头行是纯数字年份，不能被「本行无纯数值」挡掉。"""
    html = (
        "<table><tr><td></td><td>2024</td><td>2025</td><td>同比</td></tr>"
        "<tr><td>营业收入</td><td>1,234</td><td>1,567</td><td>27.0%</td></tr>"
        "<tr><td>净利润</td><td>234</td><td>345</td><td>47.4%</td></tr></table>"
    )
    layout = html_table_layout(html)

    assert layout.header == ["", "2024", "2025", "同比"]
    assert len(layout.rows) == 2


def test_long_fragment_row_is_not_treated_as_header() -> None:
    """P53-P55：首行是截断的残句（远超 20 字），不能当表头。"""
    html = (
        "<table><tr><td></td><td>易所和证券登记结算机构申请锁定;未在2个交易日内提交锁定申请的,"
        "授权董事会核实后直接向证券交易所和证券登记结算机构报送本企业的身份信息</td></tr>"
        "<tr><td>甲方</td><td>乙方</td></tr>"
        "<tr><td>丙方</td><td>丁方</td></tr></table>"
    )
    layout = html_table_layout(html)

    assert not layout.header


def test_empty_first_cell_header_needs_a_populated_first_column() -> None:
    """后续行第 0 列没有值 → 第 0 列不是数据列，首行更像残片而不是表头。"""
    html = (
        "<table><tr><td></td><td>备注</td></tr>"
        "<tr><td></td><td>说明一</td></tr>"
        "<tr><td>甲</td><td>说明二</td></tr></table>"
    )
    layout = html_table_layout(html)

    assert not layout.header

def test_single_column_group_can_be_renamed_in_layer_two() -> None:
    """上层既有 colspan 分组、又有整列一格（本次变动），下层照样能给它取新名。

    MinerU 对「一列就是一组」的列不写 colspan，若 R2 要求「下层每一列都必须落在
    某个分组区间内」，这种表就整层丢掉，压平后 `本次变动前` 会重复出现在两列上。
    """
    html = (
        '<table><tr><td rowspan="2">股份性质</td><td colspan="2">本次变动前</td>'
        '<td>本次变动</td><td colspan="2">本次变动后</td></tr>'
        '<tr><td>数量(股)</td><td>比例</td><td>送红股及资本公积金转增股本(股)</td>'
        '<td>数量(股)</td><td>比例</td></tr>'
        '<tr><td>有限售条件股份</td><td>648,851,914</td><td>21.35%</td>'
        '<td>1,297,703,828</td><td>1,946,555,742</td><td>21.35%</td></tr></table>'
    )
    layout = html_table_layout(html)

    assert len(layout.header_rows) == 2
    assert layout.header == [
        "股份性质",
        "本次变动前/数量(股)",
        "本次变动前/比例",
        "本次变动/送红股及资本公积金转增股本(股)",
        "本次变动后/数量(股)",
        "本次变动后/比例",
    ]
    assert layout.rows == [["有限售条件股份", "648,851,914", "21.35%", "1,297,703,828", "1,946,555,742", "21.35%"]]
