"""步骤 0/1/2 的验收测试：期间表头、全宽行记录、分节切分与表头继承。"""

from __future__ import annotations

from finkb.common.models import DocMeta, StitchedTable, TableBlock
from finkb.parser.markdown_blocks import html_table_layout, html_table_to_rows
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
    layout = html_table_layout(html)

    # 标题行被单独抽走，不是表头；真正的表头是紧随其后的那一行。
    assert layout.table_title == "公司及其子公司对外担保情况"
    assert layout.header == ["担保对象名称", "担保额度"]
    assert layout.rows == [["比亚迪汽车工业", "1,240,000"]]


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


# --------------------------------------------------------------------------- #
# 单行横表（kv_row）
# --------------------------------------------------------------------------- #
def test_kv_row_key_first() -> None:
    """三季报 P4_T06：键|值|键|值，两组完整配对。"""
    from finkb.parser.section_splitter import parse_kv_row

    row = [
        "报告期末普通股股东总数",
        "642,600(其中A股股东为642,496户,H股股东为104户)",
        "报告期末表决权恢复的优先股股东总数",
        "0",
    ]
    pairs, orphans = parse_kv_row(row)  # type: ignore[misc]

    assert pairs == [
        ("报告期末普通股股东总数", "642,600(其中A股股东为642,496户,H股股东为104户)"),
        ("报告期末表决权恢复的优先股股东总数", "0"),
    ]
    assert orphans == []


def test_kv_row_with_leading_orphan_value() -> None:
    """年报 P109：首格标签丢失 → 值|键|值|键|值|键|值，剥离首个孤儿值后恰好 3 对。"""
    from finkb.parser.section_splitter import parse_kv_row

    row = [
        "685,147(其中A股股东为685,034户,H股股东为113户)",
        "年度报告披露日前上一月末普通股股东总数",
        "732,853(其中A股股东为732,736户,H股股东为117户)",
        "报告期末表决权恢复的优先股股东总数",
        "0",
        "年度报告披露日前上一月末表决权恢复的优先股股东总数",
        "0",
    ]
    pairs, orphans = parse_kv_row(row)  # type: ignore[misc]

    assert len(pairs) == 3
    assert pairs[0] == ("年度报告披露日前上一月末普通股股东总数", "732,853(其中A股股东为732,736户,H股股东为117户)")
    assert pairs[1] == ("报告期末表决权恢复的优先股股东总数", "0")
    assert pairs[2] == ("年度报告披露日前上一月末表决权恢复的优先股股东总数", "0")
    assert orphans == ["685,147(其中A股股东为685,034户,H股股东为113户)"]


def test_three_cell_row_is_not_kv_row() -> None:
    """反例：3 格「键|值|键」是普通数据行（只有 1 组配对），不得判为横表。"""
    from finkb.parser.section_splitter import parse_kv_row

    assert parse_kv_row(["房屋及建筑物", "41,744,687", "尚未办完所有手续"]) is None


def test_kv_row_is_labelled_and_parsed_end_to_end() -> None:
    table = make_table(
        rows=[
            ["报告期末普通股股东总数", "642,600", "报告期末表决权恢复的优先股股东总数", "0"],
            ["前10名股东持股情况", "", "", ""],
            ["股东名称", "股东性质", "持股比例", "持股数量"],
            ["甲", "境内自然人", "10.00%", "1,000"],
        ],
        header=None,
        section_rows=[(1, "前10名股东持股情况")],
    )
    result = split_sections(table)

    first = result.tables[0]
    assert first.kind == "kv_row"
    assert first.pairs == [
        ("报告期末普通股股东总数", "642,600"),
        ("报告期末表决权恢复的优先股股东总数", "0"),
    ]
    assert first.orphan_values == []
    # 孤儿值不进入 rows
    assert first.rows == [["报告期末普通股股东总数", "642,600", "报告期末表决权恢复的优先股股东总数", "0"]]


def test_kv_row_markdown_renders_pairs() -> None:
    table = make_table(
        rows=[["报告期末普通股股东总数", "642,600", "报告期末表决权恢复的优先股股东总数", "0"]],
        header=None,
    )
    logical = split_sections(table).tables[0]
    md = logical.to_markdown()

    assert "| 键 | 值 |" in md
    assert "报告期末普通股股东总数" in md


def test_table_with_header_is_never_kv_row() -> None:
    """父表已有表头 → 单行即使凑成「键|值|键|值」也是数据行（P186_T134 情形）。

    实测反例：``房屋及建筑物 | 41,744,687 | 尚未办完所有手续`` 三格全是**值**
    （项目/账面价值/未办妥原因），但「尚未办完所有手续」不含数字，
    会被 `_cell_kind` 误判成「键」，凑出假交替。
    """
    table = make_table(
        rows=[["房屋及建筑物", "41,744,687", "尚未办完所有手续", "2025年"]],
        header=["", "账面价值", "未办妥产权证书的原因", "年度"],
    )
    logical = split_sections(table).tables[0]

    assert logical.kind == "data"
    assert logical.pairs == []
    assert logical.orphan_values == []


def test_same_row_without_header_is_kv_row() -> None:
    """同样的行，父表无表头时才会被判为横表——证明守卫是唯一差异。"""
    table = make_table(
        rows=[["房屋及建筑物", "41,744,687", "尚未办完所有手续", "2025年"]],
        header=None,
    )
    logical = split_sections(table).tables[0]

    assert logical.kind == "kv_row"


# --------------------------------------------------------------------------- #
# 步骤 1 修订：汇总块必须「又短又稀」
# --------------------------------------------------------------------------- #
HEADER_4 = ["项目", "附注七", "2025年", "2024年"]


def _sparse_rows(count: int, start: int = 0) -> list[list[str]]:
    """空列多的宽表行：4 列只有 3 个非空格（附注列留空）。"""
    return [[f"项目{start + i}", "", str(i), str(i)] for i in range(count)]


def test_long_sparse_segment_is_not_summary() -> None:
    """P130_T77 形态：后继行很稀，但整段有 9 行 —— 是数据表不是汇总块。"""
    label = "一、经营活动产生的现金流量"
    table = make_table(
        [[label, "", "", ""]] + _sparse_rows(9),
        header=HEADER_4,
        section_rows=[(0, label)],
    )
    result = split_sections(table)

    assert [t.kind for t in result.tables] == ["data"]
    assert "降级为 data" in result.cuts[0].reason
    assert result.cuts[0].kind == "data"


def test_short_sparse_segment_is_still_summary() -> None:
    """真汇总块：2 行，每行 3 个非空格 —— 必须仍然判 summary。"""
    label = "三、筹资活动产生的现金流量"
    table = make_table(
        [[label, "", "", ""]] + _sparse_rows(2),
        header=HEADER_4,
        section_rows=[(0, label)],
    )
    result = split_sections(table)

    assert [t.kind for t in result.tables] == ["summary"]
    assert "降级" not in result.cuts[0].reason


def test_dense_long_segment_is_data() -> None:
    """行数够多且每行非空格也多 —— 两个条件都不满足，当然是数据表。"""
    label = "二、投资活动产生的现金流量"
    table = make_table(
        [[label, "", "", ""]] + [[f"项目{i}", "附注", str(i), str(i)] for i in range(6)],
        header=HEADER_4,
        section_rows=[(0, label)],
    )
    result = split_sections(table)
    assert [t.kind for t in result.tables] == ["data"]
