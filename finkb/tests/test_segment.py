"""分产品 / 分行业 / 分地区结构表的解析（ADR-045）。"""

from __future__ import annotations

from finkb.fact_engine.segment import find_segment_tables, parse_segments
from finkb.fact_engine.selector import SourceTable
from finkb.fact_engine.store import FactStore

HEADER = ["项目", "营业收入", "营业成本", "毛利率", "营业收入比上年同期增减"]


def _table(rows, *, logical_id="doc#P1_T01", children=(), section_path=(), period="2025"):
    return SourceTable(
        doc_id="doc",
        company="000001",
        company_name="示例公司",
        period=period,
        report_date="2025-12-31",
        logical_id=logical_id,
        page=1,
        section="",
        header=HEADER,
        rows=rows,
        children=list(children),
        section_path=section_path,
    )


def test_parse_segments_with_marker_rows():
    table = _table(
        [
            ["分产品", "", "", "", ""],
            ["甲产品", "1,000,000,000.00", "600,000,000.00", "40.00%", "1.00%"],
            ["乙产品", "2,000,000,000.00", "1,800,000,000.00", "10.00%", "-2.00%"],
            ["合计", "3,000,000,000.00", "2,400,000,000.00", "20.00%", "0.00%"],
        ]
    )
    rows = parse_segments(table, "元")
    assert [r["segment_name"] for r in rows] == ["甲产品"] * 3 + ["乙产品"] * 3
    assert {r["dimension"] for r in rows} == {"product"}
    by_key = {(r["segment_name"], r["metric"]): r["value_norm"] for r in rows}
    assert by_key[("甲产品", "segment_revenue")] == 10.0
    assert by_key[("甲产品", "segment_gross_margin")] == 40.0


def test_parse_segments_uses_explicit_dimension():
    child = _table(
        [["甲产品", "1,000,000,000.00", "600,000,000.00", "40.00%", "1.00%"]],
        section_path=("分产品",),
    )
    rows = parse_segments(child, "元", dimension="product")
    assert {r["dimension"] for r in rows} == {"product"}
    assert len(rows) == 3


def test_find_segment_tables_accepts_section_path_marker():
    child = _table(
        [["甲产品", "1,000,000,000.00", "600,000,000.00", "40.00%", "1.00%"]],
        logical_id="doc#P1_T01#S01",
        section_path=("分产品",),
    )
    parent = _table([], children=[child])
    assert find_segment_tables([parent]) == [parent]


def test_find_segment_tables_rejects_table_without_dimension():
    parent = _table([["甲产品", "1,000,000,000.00", "600,000,000.00", "40.00%", "1.00%"]])
    assert find_segment_tables([parent]) == []


def test_segment_fact_roundtrip_is_idempotent(tmp_path):
    with FactStore(tmp_path / "facts.duckdb") as store:
        kwargs = dict(
            company="000001",
            period="2025",
            dimension="product",
            segment_name="甲产品",
            metric="segment_revenue",
            value_norm=10.0,
            unit="亿元",
            source_doc="doc",
            page_num=1,
            table_id="doc#P1_T01#S01",
        )
        store.insert_segment_fact(**kwargs)
        store.insert_segment_fact(**{**kwargs, "value_norm": 11.0})
        rows = store.segment_facts()
        assert len(rows) == 1
        assert rows[0][6] == 11.0


def test_find_segment_tables_accepts_header_marker():
    header = ["分行业", "营业收入", "营业成本", "毛利率", "营业收入比上年增减"]
    child = SourceTable(
        doc_id="doc",
        company="000001",
        company_name="示例公司",
        period="2025",
        report_date="2025-12-31",
        logical_id="doc#P1_T01#S01",
        page=1,
        section="",
        header=header,
        rows=[["甲行业", "1,000,000,000.00", "600,000,000.00", "40.00%", "1.00%"]],
        section_path=(),
    )
    parent = _table([], children=[child])
    parent.header = header
    assert find_segment_tables([parent]) == [parent]
    rows = parse_segments(child, "元", dimension="industry")
    assert {r["dimension"] for r in rows} == {"industry"}


def test_section_path_substring_is_recognized():
    child = _table(
        [["甲产品", "1,000,000,000.00", "600,000,000.00", "40.00%", "1.00%"]],
        section_path=("主营业务分产品情况",),
    )
    parent = _table([], children=[child])
    assert find_segment_tables([parent]) == [parent]


def test_alternate_revenue_cost_columns():
    header = ["煤炭品种", "销售收入", "销售成本", "毛利率"]
    table = _table([["动力煤", "2,212,320,000.00", "1,546,310,000.00", "30.10"]])
    table.header = header
    rows = parse_segments(table, "元", dimension="product")
    by_key = {(r["metric"]): r["value_norm"] for r in rows}
    assert by_key["segment_revenue"] == 22.1232
    assert by_key["segment_gross_margin"] == 30.1


def test_nested_and_subtotal_rows_are_skipped():
    table = _table(
        [
            ["分产品", "", "", "", ""],
            ["甲产品", "1,000,000,000.00", "600,000,000.00", "40.00%", "1.00%"],
            ["其中:子产品", "200,000,000.00", "100,000,000.00", "50.00%", "1.00%"],
            ["境内小计", "800,000,000.00", "500,000,000.00", "37.50%", "1.00%"],
            ["合计", "1,000,000,000.00", "600,000,000.00", "40.00%", "0.00%"],
        ]
    )
    rows = parse_segments(table, "元")
    assert [r["segment_name"] for r in rows] == ["甲产品"] * 3


def test_drop_earlier_tables_discards_prior_year_copy():
    from finkb.fact_engine.segment import _drop_earlier_tables

    header = ["项目", "营业收入", "营业成本", "毛利率", "营业收入比上年同期增减"]
    # 中年报里并排的两张同构表：本期合计 3712.81，上期合计 3011.27
    current = _table(
        [["甲产品", "371,280,948,000.00", "300,000,000,000.00", "19.20%", "1.00%"]],
        period="2025H1",
    )
    current.header = header
    earlier = _table(
        [["甲产品", "301,126,713,000.00", "240,000,000,000.00", "20.30%", "1.00%"]],
        period="2025H1",
    )
    earlier.header = header
    plan = [(current, "product", "元"), (earlier, "product", "元")]
    kept = _drop_earlier_tables(plan, {"2025H1": 3712.80948, "2024H1": 3011.26713})
    assert [item[0] for item in kept] == [current]
