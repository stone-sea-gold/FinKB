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
) -> LogicalTable:
    return LogicalTable(
        logical_id=logical_id or f"{DOC}#P{page}_T01#S01",
        parent_id=f"{DOC}#P{page}_T01",
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


def test_balance_sheet_identity_fails_on_mismatch() -> None:
    assets = make_table([["资产总计", "", "1,001"]], logical_id=f"{DOC}#P1_T01#S01")
    equity = make_table(
        [["负债合计", "", "600"], ["股东权益合计", "", "400"]],
        logical_id=f"{DOC}#P2_T01#S01",
    )
    assert not audit.check_balance_sheet([assets, equity])[0].ok
