"""审计工具的核心判据测试（``tools/audit.py``）。

这些判据决定「改前/改后」报告是否可信：单位判定错了，报告就会
把错误结论当成基线；数值指纹漏了，守恒律就形同虚设。

全部离线：只构造内存对象，不读 PDF、不调 MinerU。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import audit  # noqa: E402

from finkb.common.models import LogicalTable  # noqa: E402

DOC = "002594_2025_年报"


def make_table(
    rows: list[list[str]],
    header: list[str] | None = None,
    kind: str = "data",
    page: int = 10,
    logical_id: str | None = None,
    parent: str | None = None,
) -> LogicalTable:
    parent_id = parent or f"{DOC}#P{page}_T01"
    return LogicalTable(
        logical_id=logical_id or f"{parent_id}#S01",
        parent_id=parent_id,
        header=header,
        header_source="self" if header else "inherited",
        section_path=[],
        rows=rows,
        page_start=page,
        page_end=page,
        row_range=(0, len(rows)),
        kind=kind,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# 1. 单位归一化
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("元", "元"),
        ("人民币元", "元"),
        ("万元", "万元"),
        ("千元", "千元"),
        ("亿元", "亿元"),
        ("万港元", "万港元"),
        ("港元", "港元"),
        ("人民币", None),
    ],
)
def test_normalize_unit(token: str, expected: str | None) -> None:
    assert audit.normalize_unit(token) == expected


def test_normalize_unit_prefers_longest_token() -> None:
    """``万港元`` 不能被 ``港元`` 抢先命中，否则差 10000 倍。"""
    assert audit.normalize_unit("万港元") == "万港元"
    assert audit.normalize_unit("千港元") == "千港元"


# --------------------------------------------------------------------------- #
# 2. 数值解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("1,234", "1234"),
        ("1,234.50", "1234.5"),
        ("(1,234)", "-1234"),
        ("（1,234）", "-1234"),
        ("12.3%", "12.3%"),
        ("-5", "-5"),
        ("", None),
        ("不适用", None),
        ("2025年", None),
        ("61", "61"),
        # 「元、两位小数」口径的年报：14 位有效数字必须一位不丢。
        # 曾按 10 位有效数字截断，190,501,783,194.92 → 190,501,783,200，
        # 现金流恒等式因此凭空差 30 元（赛力斯 2025 年报）。
        ("190,501,783,194.92", "190501783194.92"),
        ("143,905,863,420.75", "143905863420.75"),
    ],
)
def test_parse_number(cell: str, expected: str | None) -> None:
    assert audit._parse_number(cell) == expected


# --------------------------------------------------------------------------- #
# 3. 数值指纹
# --------------------------------------------------------------------------- #
def test_numeric_digest_ignores_header_and_kind() -> None:
    """表头、类型、单位都是元数据——改了它们，数值指纹必须不变。"""
    rows = [["营业收入", "1,000"], ["净利润", "200"]]
    with_header = make_table(rows, header=["项目", "2025年"])
    without = make_table(rows, kind="summary")
    assert audit.numeric_digest(with_header.rows) == audit.numeric_digest(without.rows)


def test_numeric_digest_changes_when_a_value_changes() -> None:
    before = [["营业收入", "1,000"]]
    after = [["营业收入", "1,001"]]
    assert audit.numeric_digest(before) != audit.numeric_digest(after)


def test_numeric_digest_stays_at_ten_significant_digits() -> None:
    """指纹按 10 位有效数字归一——历史快照才能继续比对。

    解析要全精度（算术用），指纹要归一（跨版本比对用）。两者拆开，
    改精度就不会把所有历史快照变成「数值全变」。
    """
    assert audit.numeric_digest([["1,234.00000001"]]) == audit.numeric_digest([["1,234"]])
    assert audit.numeric_digest([["12.30%"]]) == audit.numeric_digest([["12.3%"]])


def test_numeric_digest_detects_row_reorder() -> None:
    """顺序也是信息：行序被改动必须能发现。"""
    a = [["营业收入", "1,000"], ["净利润", "200"]]
    b = [["净利润", "200"], ["营业收入", "1,000"]]
    assert audit.numeric_digest(a) != audit.numeric_digest(b)


# --------------------------------------------------------------------------- #
# 4. 单位判定链
# --------------------------------------------------------------------------- #
def decl(page: int, block: int, unit: str) -> audit.UnitDecl:
    return audit.UnitDecl(page, block, unit, f"单位：{unit}")


def test_in_table_marker_beats_scope_declaration() -> None:
    """表内写明的单位优先级最高，哪怕前面的声明是别的单位。"""
    table = make_table(
        [["营业收入(元)", "1,000"], ["总资产(元)", "5,000"]],
        header=["项目", "2025年"],
    )
    decision = audit.resolve_unit(table, [decl(1, 0, "千元")], (10, 5))
    assert decision.unit == "元"
    assert decision.rule == "in_table"


def test_scope_declaration_applies_until_next_declaration() -> None:
    """作用域语义：取**最后一个不晚于本表**的声明。"""
    decls = [decl(5, 0, "元"), decl(20, 0, "万元"), decl(30, 0, "千元")]
    table = make_table([["营业收入", "1,000"]])

    assert audit.resolve_unit(table, decls, (10, 0)).unit == "元"
    assert audit.resolve_unit(table, decls, (25, 0)).unit == "万元"
    assert audit.resolve_unit(table, decls, (40, 0)).unit == "千元"


def test_declaration_inside_the_table_block_still_applies() -> None:
    """声明写在表格首行（同一块内）时也必须命中。"""
    table = make_table([["单位：万元"], ["营业收入", "1,000"]])
    decision = audit.resolve_unit(table, [decl(10, 3, "万元")], (10, 3))
    assert decision.unit == "万元"


def test_unknown_before_first_declaration() -> None:
    """声明区之前不猜——宁可 unknown。"""
    table = make_table([["项目", "内容"]])
    decision = audit.resolve_unit(table, [decl(20, 0, "千元")], (5, 0))
    assert decision.unit is None
    assert decision.rule == "unknown"


def test_conflicting_in_table_units_fall_through_to_declaration() -> None:
    """表内标记互相矛盾时不硬选一个，交给作用域声明。"""
    table = make_table(
        [["营业收入(元)", "1,000"], ["总资产(万元)", "5,000"]],
        header=["项目", "2025年"],
    )
    decision = audit.resolve_unit(table, [decl(1, 0, "千元")], (10, 5))
    assert decision.unit == "千元"
    assert decision.rule == "scope_declaration"


def test_no_declaration_and_conflict_is_unknown() -> None:
    table = make_table(
        [["营业收入(元)", "1,000"], ["总资产(万元)", "5,000"]],
        header=["项目", "2025年"],
    )
    decision = audit.resolve_unit(table, [], (10, 5))
    assert decision.unit is None
    assert "矛盾" in decision.evidence["reason"]


# --------------------------------------------------------------------------- #
# 5. 对照报告
# --------------------------------------------------------------------------- #
def snapshot_of(tables: dict[str, dict]) -> dict:
    return {"doc_id": DOC, "created_at": "t0", "tables": tables}


def entry(kind: str = "data", unit: str | None = "千元", rows: int = 3) -> dict:
    return {
        "kind": kind,
        "header": ["项目", "2025年"],
        "header_source": "inherited",
        "section_path": [],
        "unit": unit,
        "unit_rule": "scope_declaration" if unit else "unknown",
        "unit_evidence": {},
        "row_count": rows,
        "row_range": [0, rows],
        "page_start": 10,
        "page_end": 10,
        "numeric_digest": "abc",
        "facts": {"rows": rows, "ncols": 2, "max_nonempty_per_row": 2,
                  "has_header": True, "header_first_empty": False,
                  "numeric_cells": 2, "nonempty_cells": 4, "first_row": ["项目", "2025年"]},
    }


def test_diff_detects_field_change() -> None:
    before = snapshot_of({"a#S01": entry(kind="summary", rows=10)})
    after = snapshot_of({"a#S01": entry(kind="data", rows=10)})
    markdown, touched = audit.diff_snapshots(before, after)

    assert touched == ["a#S01"]
    assert "summary" in markdown and "data" in markdown


def test_diff_reports_no_change_when_identical() -> None:
    snap = snapshot_of({"a#S01": entry()})
    markdown, touched = audit.diff_snapshots(snap, snap)
    assert touched == []
    assert "没有任何变化" in markdown


def test_diff_reports_added_and_removed() -> None:
    before = snapshot_of({"a#S01": entry(), "b#S01": entry()})
    after = snapshot_of({"a#S01": entry(), "c#S01": entry()})
    markdown, _ = audit.diff_snapshots(before, after)

    assert "新增的表" in markdown and "c#S01" in markdown
    assert "消失的表" in markdown and "b#S01" in markdown


def test_diff_flags_numeric_change_in_verdict_table() -> None:
    before = snapshot_of({"a#S01": entry(kind="summary")})
    after_entry = entry(kind="data")
    after_entry["numeric_digest"] = "different"
    after = snapshot_of({"a#S01": after_entry})

    markdown, _ = audit.diff_snapshots(before, after)
    assert "数值变了" in markdown


# --------------------------------------------------------------------------- #
# 6. 表头候选（P1-1 人工确认范围）
# --------------------------------------------------------------------------- #
def test_looks_like_header_for_glossary() -> None:
    assert audit._looks_like_header(["释义项", "指", "释义内容"])


def test_data_row_of_glossary_is_also_flagged() -> None:
    """释义表的数据行与表头同构——这正是 P1-1 只能人工确认的原因。"""
    assert audit._looks_like_header(["比亚迪", "指", "比亚迪股份有限公司"])


def test_numeric_row_is_not_header_candidate() -> None:
    assert not audit._looks_like_header(["营业收入", "1,234"])


def test_year_row_is_header_candidate() -> None:
    """``2025年 | 2024年`` 本身就是表头——P249_T247#S01 漏掉的正是这种。"""
    assert audit._looks_like_header(["2025年", "2024年"])


def test_single_cell_is_not_header_candidate() -> None:
    assert not audit._looks_like_header(["流动资产"])
    assert not audit._looks_like_header([])


# --------------------------------------------------------------------------- #
# 7. 算术恒等式
# --------------------------------------------------------------------------- #
def cash_flow_table(net: str) -> LogicalTable:
    return make_table(
        [
            ["经营活动现金流入小计", "", "100", "90"],
            ["经营活动现金流出小计", "", "40", "30"],
            ["经营活动产生的现金流量净额", "62", net, "60"],
        ],
        header=["项目", "附注七", "2025年", "2024年"],
    )


def test_cash_flow_identity_passes() -> None:
    checks = audit.check_cash_flow([cash_flow_table("60")])
    assert [c for c in checks if c.name == "现金流量表·经营活动"][0].ok


def test_cash_flow_identity_fails_on_mismatch() -> None:
    checks = audit.check_cash_flow([cash_flow_table("61")])
    assert not [c for c in checks if c.name == "现金流量表·经营活动"][0].ok


def test_cash_flow_identity_holds_for_yuan_with_cents() -> None:
    """「元、两位小数」口径：14 位有效数字被截成 10 位就会凭空差 30 元。"""
    table = make_table(
        [
            ["经营活动现金流入小计", "", "190,501,783,194.92"],
            ["经营活动现金流出小计", "", "161,588,039,865.68"],
            ["经营活动产生的现金流量净额", "", "28,913,743,329.24"],
        ],
        header=["项目", "附注", "2025年度"],
    )
    check = [c for c in audit.check_cash_flow([table]) if c.name == "现金流量表·经营活动"][0]
    assert check.ok, check.detail


def test_cash_flow_ignores_attachment_column() -> None:
    """「附注七」列里也有数字（62），不能被当成金额。"""
    checks = audit.check_cash_flow([cash_flow_table("60")])
    check = [c for c in checks if c.name == "现金流量表·经营活动"][0]
    assert check.ok
    assert "60" in check.detail


def test_balance_sheet_identity_passes() -> None:
    assets = make_table([["资产总计", "", "1,000"]], logical_id=f"{DOC}#P1_T01#S01")
    equity = make_table(
        [["负债合计", "", "600"], ["股东权益合计", "", "400"]],
        logical_id=f"{DOC}#P2_T01#S01",
    )
    check = audit.check_balance_sheet([assets, equity])[0]
    assert check.ok


def test_balance_sheet_handles_new_standard_equity_label_across_sections() -> None:
    """新准则标签 + 一张物理表被切成多段：资产 #S02、负债 #S04、权益 #S05。

    旧写法要求「同一张逻辑表里同时有负债合计和股东权益合计」，这种排版下
    直接找不到（赛力斯 2025 年报：资产 143,905,863,420.75 = 负债
    102,047,535,793.91 + 所有者权益 41,858,327,626.84）。
    """
    parent = f"{DOC}#P80-P82_T82"
    assets = make_table([["资产总计", "", "1,000"]], logical_id=f"{parent}#S02", parent=parent)
    liability = make_table([["负债合计", "", "600"]], logical_id=f"{parent}#S04", parent=parent)
    equity = make_table(
        [["所有者权益(或股东权益)合计", "", "400"]],
        logical_id=f"{parent}#S05",
        parent=parent,
    )
    check = audit.check_balance_sheet([assets, liability, equity])[0]
    assert check.ok, check.detail


def test_balance_sheet_prefers_sibling_sections_over_a_parent_only_statement() -> None:
    """同一份文档里合并报表与母公司报表并存时，取与资产总计同表的那一段。"""
    consolidated = f"{DOC}#P80-P82_T82"
    parent_only = f"{DOC}#P82-P84_T83"
    assets = make_table(
        [["资产总计", "", "1,000"]], logical_id=f"{consolidated}#S02", parent=consolidated
    )
    liab = make_table([["负债合计", "", "600"]], logical_id=f"{consolidated}#S04", parent=consolidated)
    equity = make_table(
        [["所有者权益(或股东权益)合计", "", "400"]],
        logical_id=f"{consolidated}#S05",
        parent=consolidated,
    )
    # 母公司报表排在后面，数值不同——不能被误取
    other_assets = make_table(
        [["资产总计", "", "900"]], logical_id=f"{parent_only}#S02", parent=parent_only
    )
    other_liab = make_table(
        [["负债合计", "", "500"]], logical_id=f"{parent_only}#S04", parent=parent_only
    )
    other_equity = make_table(
        [["所有者权益(或股东权益)合计", "", "400"]],
        logical_id=f"{parent_only}#S05",
        parent=parent_only,
    )
    check = audit.check_balance_sheet(
        [assets, liab, equity, other_assets, other_liab, other_equity]
    )[0]
    assert check.ok, check.detail


def test_balance_sheet_identity_fails_on_mismatch() -> None:
    assets = make_table([["资产总计", "", "1,001"]], logical_id=f"{DOC}#P1_T01#S01")
    equity = make_table(
        [["负债合计", "", "600"], ["股东权益合计", "", "400"]],
        logical_id=f"{DOC}#P2_T01#S01",
    )
    assert not audit.check_balance_sheet([assets, equity])[0].ok


# --------------------------------------------------------------------------- #
# 页码区间证据（跨页表锚点回填）
# --------------------------------------------------------------------------- #
def test_normalize_strips_whitespace_and_fullwidth_punctuation() -> None:
    assert audit._normalize(" 比亚迪 ：2025 年 （含税）") == "比亚迪:2025年(含税)"


def test_placeholder_pages_map_to_the_table_above() -> None:
    content_list = [
        {"type": "text", "text": "说明", "page_idx": 0},
        {"type": "table", "table_body": "<table></table>", "page_idx": 0},
        {"type": "table", "table_body": "", "page_idx": 1},
        {"type": "table", "table_body": "", "page_idx": 2},
        {"type": "table", "table_body": "<table></table>", "page_idx": 2},
        {"type": "table", "table_body": "", "page_idx": 3},
    ]

    assert audit._placeholder_pages(content_list) == {1: [2, 3], 3: [4]}


def test_placeholder_on_anchor_page_is_not_counted() -> None:
    content_list = [
        {"type": "table", "table_body": "<table></table>", "page_idx": 0},
        {"type": "table", "table_body": "", "page_idx": 0},
    ]

    assert audit._placeholder_pages(content_list) == {}


def test_page_span_verdict_prefers_in_range_hits() -> None:
    span = audit.PageSpan(
        table_id=f"{DOC}#P9-P10_T05",
        page_start=9,
        page_end=10,
        row_count=3,
        logical_count=1,
        placeholder_pages=[10],
        total=3,
        hits_in=[9, 10],
        hits_outside=0,
        misses=0,
    )

    assert span.verdict.startswith("✓")


def test_page_span_verdict_flags_only_outside_hits() -> None:
    span = audit.PageSpan(
        table_id=f"{DOC}#P9-P10_T05",
        page_start=9,
        page_end=10,
        row_count=3,
        logical_count=1,
        placeholder_pages=[10],
        total=3,
        hits_in=[],
        hits_outside=2,
        misses=1,
    )

    assert span.verdict.startswith("⚠")


def test_table_key_ignores_page_range() -> None:
    assert audit._table_key("d#P94_T57#S01") == audit._table_key("d#P94-P103_T57#S01")


def test_rename_pairing_requires_same_table_sequence() -> None:
    """内容完全相同的两张不同表不能配成「改号」——合并报表与母公司报表常有这种。"""
    before = snapshot_of({"d#P1_T01#S01": entry()})
    after = snapshot_of({"d#P1_T05#S01": entry()})

    markdown, _ = audit.diff_snapshots(before, after)

    assert "改号 0 张" in markdown
    assert "新增的表" in markdown and "消失的表" in markdown


def test_rename_pairing_matches_same_table_after_page_range_change() -> None:
    before = snapshot_of({"d#P94_T57#S01": entry()})
    after = snapshot_of({"d#P94-P103_T57#S01": entry()})

    markdown, _ = audit.diff_snapshots(before, after)

    assert "改号 1 张" in markdown


# --------------------------------------------------------------------------- #
# 现金流量表：取哪一列、空单元格怎么算
# --------------------------------------------------------------------------- #
def test_amount_column_is_the_leftmost_money_column() -> None:
    """两期并排时，上期那列可能更满，不能按「数值最多」选。

    000625 半年报 P42-P43_T37#S02：本期（第 1 列）有两格空白，上期（第 2 列）
    满格；按「最多」会取到上期，把上期净额当成本期。
    """
    table = make_table(
        [
            ["取得投资收益收到的现金", "418,072,106.70", "298,979,999.95"],
            ["收到其他与投资活动有关的现金", "", "5,550,500,000.00"],
            ["投资活动现金流入小计", "572,123,897.11", "5,862,228,015.54"],
            ["投资活动现金流出小计", "3,105,615,678.84", "2,818,618,769.37"],
            ["投资活动产生的现金流量净额", "-2,533,491,781.73", "3,043,609,246.17"],
        ],
        header=["项目", "2025年半年度", "2024年半年度"],
    )

    assert audit._first_numeric_column(table) == 1
    check = audit._check_cash_flow_table("投资活动", table)
    assert check.ok, check.detail


def test_note_number_column_is_not_taken_as_money() -> None:
    """「附注七」列里也有数字（62、61），但不能被当成金额列。"""
    table = make_table(
        [
            ["销售商品、提供劳务收到的现金", "61", "59,135,544", "133,453,873"],
            ["经营活动现金流入小计", "62", "861,740,107", "802,604,563"],
            ["经营活动现金流出小计", "", "802,604,563", "700,000,000"],
            ["经营活动产生的现金流量净额", "", "59,135,544", "102,604,563"],
        ],
        header=["项目", "附注七", "2025年", "2024年"],
    )

    assert audit._first_numeric_column(table) == 2


def test_blank_subtotal_counts_as_zero() -> None:
    """母公司现金流量表整段留空（本期没有筹资流入），PDF 原文就是空的。"""
    table = make_table(
        [
            ["筹资活动现金流入小计", "", ""],
            ["筹资活动现金流出小计", "76,666,556.36", "3,453,081,963.24"],
            ["筹资活动产生的现金流量净额", "-76,666,556.36", "-3,453,081,963.24"],
        ],
        header=["项目", "2025年半年度", "2024年半年度"],
    )

    assert audit._amount_in_column(table, "筹资活动现金流入小计", 1) == 0.0
    assert audit._amount_in_column(table, "不存在的行", 1) is None
    check = audit._check_cash_flow_table("筹资活动", table)
    assert check.ok, check.detail


def test_digit_signature_skips_short_numbers() -> None:
    """年份这类短数字在整页文本里到处命中，不参与回查。"""
    assert audit._digit_signature("2025年") is None
    assert audit._digit_signature("1,234") is None
    assert audit._digit_signature("190,501,783,194.92") == "19050178319492"


def test_digit_signature_ignores_formatting() -> None:
    """千分位、负号、百分号都不影响数字指纹。"""
    assert audit._digit_signature("-2,533,491,781.73") == audit._digit_signature(
        "(2,533,491,781.73)"
    )

def test_pipeline_fingerprint_is_stable_hex():
    from finkb.version import pipeline_fingerprint

    value = pipeline_fingerprint()
    assert value == pipeline_fingerprint()
    assert len(value) == 12 and all(ch in "0123456789abcdef" for ch in value)


def test_parse_fingerprint_flags_missing_stamp(tmp_path):
    (tmp_path / "report.json").write_text("{}", encoding="utf-8")
    check = audit._check_fingerprint(tmp_path)
    assert not check.ok
    assert "无版本戳" in check.detail


def test_parse_fingerprint_flags_old_stamp(tmp_path):
    (tmp_path / "report.json").write_text(
        '{"pipeline_fingerprint": "000000000000"}', encoding="utf-8"
    )
    check = audit._check_fingerprint(tmp_path)
    assert not check.ok
    assert "需重跑" in check.detail


def test_parse_fingerprint_accepts_current_stamp(tmp_path):
    from finkb.version import pipeline_fingerprint

    (tmp_path / "report.json").write_text(
        '{"pipeline_fingerprint": "%s"}' % pipeline_fingerprint(), encoding="utf-8"
    )
    assert audit._check_fingerprint(tmp_path).ok


def test_recall_haystack_spans_table_pages_and_text_blocks() -> None:
    """一张跨 1–2 页的表，它的数字要同时出现在两页的「可对上」集合里。"""
    from types import SimpleNamespace

    table = SimpleNamespace(page_start=1, page_end=2, rows=[["12,345,678.90", "1"]])
    pages = [
        SimpleNamespace(number=1, blocks=[SimpleNamespace(text="补充 7,654,321 元")]),
        SimpleNamespace(number=2, blocks=[]),
    ]
    hay = audit._page_haystacks(pages, [table])

    assert "12345678.90" in hay[1]
    assert "12345678.90" in hay[2]
    assert "7654321" in hay[1]


def test_recall_report_says_when_everything_matches() -> None:
    report = audit.render_recall_report(
        {"doc_id": "d", "total": 5, "covered": 5, "elsewhere": 0, "misses": []}
    )
    assert "全部对上" in report
    assert "# 原文召回：d" in report


def test_bad_grouping_catches_glued_numbers() -> None:
    """``602,855430,242`` 是两个数字粘在一起，任何一个读出来都是错的。"""
    import audit

    assert audit._has_bad_grouping("602,855430,242")
    assert audit._has_bad_grouping("0,01%")
    assert not audit._has_bad_grouping("602,855")
    assert not audit._has_bad_grouping("1,234,567.89")
    assert not audit._has_bad_grouping("123")


def test_balance_sheet_tolerates_rounding_but_not_a_misread_digit() -> None:
    """报表四舍五入允许差几块钱；读错一位（差 100 元）必须报错。"""
    import audit

    def sheet(assets: int, liab: int, equity: int) -> list:
        def table(name: str, label: str, value: int):
            return audit.LogicalTable(
                logical_id=name, parent_id=name, header=["项目", "金额"],
                header_source="self", section_path=[], rows=[[label, f"{value:,}"]],
                page_start=1, page_end=1, row_range=(0, 1), kind="data",
            )
        return [
            table("a", "资产总计", assets),
            table("b", "负债合计", liab),
            table("c", "所有者权益合计", equity),
        ]

    rounded = audit.check_balance_sheet(sheet(214_943_407_093, 105_259_714_163, 109_683_692_932))
    assert all(check.ok for check in rounded)
    broken = audit.check_balance_sheet(sheet(214_943_407_193, 105_259_714_163, 109_683_692_932))
    assert not all(check.ok for check in broken)


def test_cash_flow_accepts_outflows_printed_as_negative() -> None:
    """银行把流出印成负数 (596,456)，制造业印成正数 596,456，两种都要对。"""
    import audit

    def statement(outflow_cell: str):
        return audit.LogicalTable(
            logical_id="d#P1_T01#S01", parent_id="d#P1_T01", header=["项目", "本期"],
            header_source="self", section_path=[],
            rows=[
                ["经营活动现金流入小计", "730,917"],
                ["经营活动现金流出小计", outflow_cell],
                ["经营活动产生的现金流量净额", "134,461"],
            ],
            page_start=1, page_end=1, row_range=(0, 3), kind="data",
        )

    assert audit.check_cash_flow([statement("596,456")])[0].ok
    assert audit.check_cash_flow([statement("-596,456")])[0].ok
    assert audit.check_cash_flow([statement("-596,457")])[0].ok is False


def test_unaccounted_rows_ignores_section_and_repeated_header(tmp_path) -> None:
    """分节标题行既不进表也不算丢——只有既没进表、也不是分节标题的才算。"""
    from finkb.common.models import LogicalTable, StitchedTable
    import audit

    table = StitchedTable(
        table_id="d#P1_T01",
        parent_id="d#P1_T01",
        doc_id="d",
        page_start=1,
        page_end=1,
        header=["项目", "金额"],
        source_tables=[],
        rows=[["项目", "金额"], ["其中：", ""], ["营业成本", "60"]],
        section_rows=[(1, "其中：")],
    )
    logical = [
        LogicalTable(
            logical_id="d#P1_T01#S01",
            parent_id="d#P1_T01",
            header=["项目", "金额"],
            header_source="self",
            section_path=[],
            rows=[],
            page_start=1,
            page_end=1,
            row_range=(0, 3),
        )
    ]
    parsed = tmp_path / "d"
    parsed.mkdir()
    (parsed / "content_list.json").write_text("[]", encoding="utf-8")
    original = audit._analyze
    audit._analyze = lambda _dir: (None, [], [table], logical)
    try:
        assert audit.find_unaccounted_rows(parsed) == []
        table.rows.append(["营业外收入", "5"])
        assert [item["row"] for item in audit.find_unaccounted_rows(parsed)] == [3]
    finally:
        audit._analyze = original


