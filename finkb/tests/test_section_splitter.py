"""步骤 0/1/2 的验收测试：期间表头、全宽行记录、分节切分与表头继承。"""

from __future__ import annotations

from finkb.common.models import DocMeta, StitchedTable, TableBlock
from finkb.parser.markdown_blocks import html_table_to_rows
from finkb.parser.section_splitter import (
    SplitOptions,
    _looks_like_heading,
    split_sections,
)

DOC = "002594_2025_年报"


def make_table(
    rows: list[list[str]],
    header: list[str] | None = None,
    section_rows: list[tuple[int, str]] | None = None,
    page: int = 1,
) -> StitchedTable:
    return StitchedTable(
        doc_id=DOC,
        header=header,
        rows=rows,
        page_start=page,
        page_end=page,
        source_tables=1,
        parent_id=f"{DOC}#P{page}_T01",
        table_id=f"{DOC}#P{page}_T01",
        section_rows=section_rows or [],
    )


# --------------------------------------------------------------------------- #
# 步骤 0：表头识别
# --------------------------------------------------------------------------- #
def test_period_header_detected_by_lookahead() -> None:
    """P137 形态：期间表头 + 紧邻的小计行（纯文本），必须靠向前探测识别。"""
    html = (
        "<table>"
        "<tr><td></td><td>2025年</td><td>2024年</td></tr>"
        "<tr><td>一、经营活动产生的现金流量:</td><td></td><td></td></tr>"
        "<tr><td>销售商品、提供劳务收到的现金</td><td>3,741,844</td><td>4,055,589</td></tr>"
        "</table>"
    )
    rows, is_header, _ = html_table_to_rows(html)

    # 注意：html_table_to_rows 返回完整行（含表头），剥离由调用方负责
    assert is_header is True
    assert rows[0] == ["", "2025年", "2024年"]
    assert len(rows) == 3


def test_fullwidth_single_cell_is_not_a_header() -> None:
    """P89 形态：首行是全宽表标题（只有一个非空格），不能被当成表头。"""
    html = (
        "<table>"
        '<tr><td colspan="2">公司及其子公司对外担保情况</td></tr>'
        "<tr><td>担保对象名称</td><td>担保额度</td></tr>"
        "<tr><td>比亚迪汽车工业</td><td>1,240,000</td></tr>"
        "</table>"
    )
    rows, is_header, _ = html_table_to_rows(html)

    assert is_header is False
    assert rows[0][0] == "公司及其子公司对外担保情况"


def test_colon_terminated_first_row_is_header() -> None:
    """P118 形态：整表无数值的纯文本表，靠「列名都以冒号结尾」识别表头。"""
    html = (
        "<table>"
        "<tr><td>关键审计事项:</td><td>该事项在审计中是如何应对:</td></tr>"
        '<tr><td colspan="2">收入确认</td></tr>'
        "<tr><td>长文本内容</td><td>长文本内容</td></tr>"
        "</table>"
    )
    rows, is_header, _ = html_table_to_rows(html)

    assert is_header is True
    assert rows[0] == ["关键审计事项:", "该事项在审计中是如何应对:"]


def test_numeric_first_row_is_not_header() -> None:
    """续接表首行是数据（含数值）时不得判为表头。"""
    html = (
        "<table>"
        "<tr><td>债券</td><td>019742</td><td>113,357,000.00</td></tr>"
        "<tr><td>股票</td><td>301358</td><td>74,694,000.00</td></tr>"
        "</table>"
    )
    _, is_header, _ = html_table_to_rows(html)
    assert is_header is False


# --------------------------------------------------------------------------- #
# 步骤 1：全宽行记录
# --------------------------------------------------------------------------- #
def test_full_width_rows_are_recorded_with_data_index() -> None:
    html = (
        "<table>"
        "<tr><td>项目</td><td>金额</td></tr>"
        "<tr><td>甲</td><td>1.00</td></tr>"
        '<tr><td colspan="2">子公司对子公司的担保情况</td></tr>'
        "<tr><td>乙</td><td>2.00</td></tr>"
        "</table>"
    )
    rows, is_header, section_rows = html_table_to_rows(html)

    assert is_header is True
    assert len(rows) == 4
    # 剔除表头后：「甲」是数据行 0，「子公司…」是数据行 1
    assert section_rows == [(1, "子公司对子公司的担保情况")]


def test_no_full_width_rows_yields_empty_list() -> None:
    html = "<table><tr><td>项目</td><td>金额</td></tr><tr><td>甲</td><td>1.00</td></tr></table>"
    _, _, section_rows = html_table_to_rows(html)
    assert section_rows == []


def test_table_block_carries_section_rows() -> None:
    block = TableBlock(
        meta=DocMeta(DOC, 1),
        header=["项目", "金额"],
        rows=[["甲", "1.00"], ["子公司担保", "", ""]],
        section_rows=[(1, "子公司担保")],
    )
    assert block.section_rows == [(1, "子公司担保")]


# --------------------------------------------------------------------------- #
# 步骤 2：切分与表头继承
# --------------------------------------------------------------------------- #
def test_no_sections_returns_single_table() -> None:
    table = make_table([["甲", "1.00"], ["乙", "2.00"]], header=["项目", "金额"])
    result = split_sections(table)

    assert result.count == 1
    assert result.tables[0].logical_id.endswith("#S01")
    assert result.tables[0].section_path == []


def test_sections_inherit_parent_header() -> None:
    """核心：切分后每段都必须继承父表表头，否则期间语义丢失。"""
    table = make_table(
        rows=[
            ["一、经营活动产生的现金流量:", "", ""],
            ["销售商品收到的现金", "3,741,844", "4,055,589"],
            ["购买商品支付的现金", "2,999,080", "3,162,303"],
            ["二、投资活动产生的现金流量:", "", ""],
            ["取得投资收益收到的现金", "53,154", "7,989,708"],
        ],
        header=["", "2025年", "2024年"],
        section_rows=[(0, "一、经营活动产生的现金流量:"), (3, "二、投资活动产生的现金流量:")],
    )
    result = split_sections(table)

    assert result.count == 2
    first, second = result.tables
    assert first.header == ["", "2025年", "2024年"]
    assert first.header_source == "inherited"
    assert first.section_path == ["一、经营活动产生的现金流量:"]
    assert first.rows == [["销售商品收到的现金", "3,741,844", "4,055,589"],
                          ["购买商品支付的现金", "2,999,080", "3,162,303"]]
    assert second.rows == [["取得投资收益收到的现金", "53,154", "7,989,708"]]
    assert second.section_path == ["二、投资活动产生的现金流量:"]
    assert second.header == ["", "2025年", "2024年"]


def test_section_with_own_header_uses_it() -> None:
    """P89 形态：每段自带表头 → header_source=self。"""
    table = make_table(
        rows=[
            ["甲", "1", "x"],
            ["担保对象名称", "担保额度", "担保期"],   # 自有表头
            ["乙", "2", "3年"],
        ],
        header=None,
        section_rows=[(0, "公司对子公司的担保情况")],
    )
    result = split_sections(table)

    assert result.count == 1
    section = result.tables[0]
    assert section.section_path == ["公司对子公司的担保情况"]
    assert section.header == ["担保对象名称", "担保额度", "担保期"]
    assert section.header_source == "self"
    assert section.rows == [["乙", "2", "3年"]]


def test_footnote_is_not_a_split_point() -> None:
    table = make_table(
        rows=[
            ["甲", "1.00"],
            ["注1:此数包括王传福先生持有的3,000,000股H股,以及夏佐全先生持有的585,000股H股", "", ""],
        ],
        header=["项目", "金额"],
        section_rows=[(1, "注1:此数包括王传福先生持有的3,000,000股H股,以及夏佐全先生持有的585,000股H股")],
    )
    result = split_sections(table)

    assert result.count == 1
    assert result.cuts[0].kind == "footnote"


def test_narrow_table_requires_heading_marker() -> None:
    """窄表上「非空≤4」恒真，只认冒号/中文序号，避免误切。"""
    table = make_table(
        rows=[["收入确认"], ["一大段说明文字"], ["应收账款"]],
        header=["关键审计事项:", "应对:"],
    )
    # 人工注入候选：无冒号无序号 → 不该切开
    from finkb.parser.section_splitter import _collect_cut_candidates

    assert _collect_cut_candidates(table, table.rows) == []


def test_narrow_table_splits_on_numbered_heading() -> None:
    table = make_table(
        rows=[
            ["一、经营活动产生的现金流量:", ""],
            ["销售商品收到的现金", "1.00"],
            ["二、投资活动产生的现金流量:", ""],
            ["收回投资收到的现金", "2.00"],
        ],
        header=["", "2025年"],
    )
    result = split_sections(table)

    assert result.count == 2
    assert result.tables[0].section_path == ["一、经营活动产生的现金流量:"]
    assert result.tables[0].header == ["", "2025年"]
    assert result.tables[1].section_path == ["二、投资活动产生的现金流量:"]


def test_all_sections_have_non_empty_header_when_parent_has_one() -> None:
    """父表有表头时，任何继承段都不能出现空表头。"""
    table = make_table(
        rows=[["第一部分:", "", ""], ["甲", "1", ""], ["第二部分:", "", ""], ["乙", "2", ""]],
        header=["项目", "金额", "备注"],
        section_rows=[(0, "第一部分:"), (2, "第二部分:")],
    )
    result = split_sections(table)

    for section in result.tables:
        assert section.header == ["项目", "金额", "备注"], "切分不得丢失表头"


def test_looks_like_heading_variants() -> None:
    assert _looks_like_heading("一、经营活动产生的现金流量:")
    assert _looks_like_heading("（1）主营业务")
    assert _looks_like_heading("列示为:")
    assert not _looks_like_heading("收入确认")
    assert not _looks_like_heading("销售商品、提供劳务收到的现金")
