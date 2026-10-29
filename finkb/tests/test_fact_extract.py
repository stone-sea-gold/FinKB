"""L1 抽取链路的离线测试：选表、期间映射、逐字核对、单位推断。

全部不联网：LLM 调用只在 ``extract_document`` 里，这里测的是它前后的纯函数。
"""

from __future__ import annotations

from finkb.fact_engine.extractor import column_periods
from finkb.fact_engine.ingest import infer_unit
from finkb.fact_engine.metric_dict import Metric
from finkb.fact_engine.selector import SourceTable, select_tables
from finkb.fact_engine.verifier import parse_number, verify

REVENUE = Metric(
    metric_id="revenue", metric_name="营业收入", unit_default="亿元", category="盈利", aliases=["营业总收入"]
)


def make_table(rows, header, section="", doc_id="000001_2025_年报", kind_hint=""):
    return SourceTable(
        doc_id=doc_id,
        company="000001",
        company_name="测试公司",
        period="2025" if "年报" in doc_id else "2025H1",
        report_date="2026-03-01",
        logical_id=kind_hint or "t1",
        page=1,
        section=section,
        header=header,
        rows=rows,
    )


def test_parse_number_variants():
    assert parse_number("1,234.5") == 1234.5
    assert parse_number("15.31%") == 15.31
    assert parse_number("803,964,958") == 803964958.0
    assert parse_number("-") is None
    assert parse_number("—") is None
    assert parse_number("(1,234)") == -1234.0


def test_column_periods_annual_and_half():
    assert column_periods(["", "2025年", "2024年(经重述)"], "2025") == {1: "2025", 2: "2024"}
    assert column_periods(["资产", "2025年6月30日", "2024年12月31日"], "2025H1") == {
        1: "2025H1",
        2: "2024",
    }
    assert column_periods(["", "本期金额", "上期金额", "本年比上年增减"], "2025") == {
        1: "2025",
        2: "2024",
    }
    assert column_periods(["", "本报告期", "上年同期"], "2025Q3") == {1: "2025Q3", 2: "2024Q3"}


def test_verify_ok_and_levels():
    table = make_table(
        [["营业收入", "1,234.5", "1,000.0"]],
        ["项目", "2025年", "2024年"],
        section="合并利润表",
    )
    metrics = {"revenue": REVENUE}
    periods = {1: "2025", 2: "2024"}

    exact, reason = verify(
        {"metric_id": "revenue", "row": 0, "col": 1, "value_raw": "1,234.5"}, table, metrics, periods, "元"
    )
    assert reason is None and exact is not None
    assert exact.match_level == "exact"
    assert abs(exact.value_norm - 1234.5 / 1e8) < 1e-6

    numeric, _ = verify(
        {"metric_id": "revenue", "row": 0, "col": 1, "value_raw": "1234.5"}, table, metrics, periods, "元"
    )
    assert numeric is not None and numeric.match_level == "normalized"


def test_verify_rejects_wrong_cell_and_bad_metric():
    table = make_table([["营业收入", "1,234.5"]], ["项目", "2025年"], section="合并利润表")
    metrics = {"revenue": REVENUE}
    periods = {1: "2025"}

    _, reason = verify(
        {"metric_id": "revenue", "row": 0, "col": 1, "value_raw": "9,999"}, table, metrics, periods, "元"
    )
    assert reason == "literal_not_found"

    _, reason = verify(
        {"metric_id": "no_such", "row": 0, "col": 1, "value_raw": "1,234.5"}, table, metrics, periods, "元"
    )
    assert reason == "metric_not_in_dict"

    _, reason = verify(
        {"metric_id": "revenue", "row": 0, "col": 9, "value_raw": "1"}, table, metrics, periods, "元"
    )
    assert reason == "schema_invalid"

    _, reason = verify({"row": 0}, table, metrics, periods, "元")
    assert reason == "schema_invalid"


def test_kind_and_select():
    bs_rows = [["资产总计", "100"], ["负债合计", "40"], ["所有者权益合计", "60"]] + [
        [f"科目{i}", "1"] for i in range(15)
    ]
    is_rows = [["营业收入", "100"], ["净利润", "10"]] + [[f"项目{i}", "1"] for i in range(20)]
    cf_rows = [["经营活动产生的现金流量净额", "5"]] + [[f"现金流{i}", "1"] for i in range(8)]
    summary_rows = [["营业收入", "100", "90"]] + [[f"指标{i}", "1", "1"] for i in range(8)]

    bs = make_table(bs_rows, ["项目", "2025年"], section="合并资产负债表", kind_hint="bs")
    is_ = make_table(is_rows, ["项目", "2025年"], section="合并利润表", kind_hint="is")
    cf = make_table(cf_rows, ["项目", "2025年"], section="合并现金流量表", kind_hint="cf")
    summary = make_table(
        summary_rows, ["", "2025年", "2024年"], section="六、主要会计数据和财务指标", kind_hint="summary"
    )

    assert (bs.kind, is_.kind, cf.kind, summary.kind) == ("bs", "is", "cf", "summary")
    picked = select_tables([summary, bs, is_, cf])
    assert {t.logical_id for t in picked} == {"summary", "bs", "is", "cf"}


def test_infer_unit_from_summary_vs_income():
    summary = make_table(
        [["营业收入", "1,000,000"]], ["", "2025年"], section="主要会计数据", kind_hint="summary"
    )
    income = make_table(
        [["营业收入", "1,000"]] + [[f"项目{i}", "1"] for i in range(20)] + [["净利润", "1"]],
        ["项目", "2025年"],
        section="合并利润表",
        kind_hint="is",
    )
    assert infer_unit([summary, income]) == "千元"

    income_same = make_table(
        [["营业收入", "1,000,000"]] + [[f"项目{i}", "1"] for i in range(20)] + [["净利润", "1"]],
        ["项目", "2025年"],
        section="合并利润表",
        kind_hint="is",
    )
    assert infer_unit([summary, income_same]) == "元"
    assert infer_unit([income_same]) is None


def test_column_periods_q3_summary_keeps_single_quarter_and_cumulative():
    header = ["项目", "本报告期", "本报告期比上年同期增减", "年初至报告期末", "年初至报告期末比上年同期增减"]
    assert column_periods(header, "2025Q3", "summary") == {1: "2025Q3", 3: "2025Q1-Q3"}


def test_column_periods_q3_income_is_cumulative():
    # 002594 三季报利润表只写「本期发生额」，口径其实是 1–9 月累计
    assert column_periods(["项目", "本期发生额", "上期发生额(经重述)"], "2025Q3", "is") == {
        1: "2025Q1-Q3",
        2: "2024Q1-Q3",
    }


def test_column_periods_q1_uses_quarter_labels():
    assert column_periods(["项目", "本报告期", "上年同期"], "2025Q1", "summary") == {
        1: "2025Q1",
        2: "2024Q1",
    }


def test_column_periods_q3_balance_sheet_point_in_time():
    assert column_periods(["项目", "期末余额", "期初余额"], "2025Q3", "bs") == {1: "2025Q3", 2: "2024"}


def test_infer_unit_uses_cumulative_revenue_for_q3():
    summary = make_table(
        [["营业收入", "48,132,697,948.00", "15.75", "110,534,420,761.80", "3.67"]],
        ["项目", "本报告期", "增减", "年初至报告期末", "增减"],
        section="主要会计数据",
        doc_id="601127_2025_三季报",
        kind_hint="summary",
    )
    income = make_table(
        [["一、营业总收入", "110,534,420,761.80"]]
        + [[f"项目{i}", "1"] for i in range(20)]
        + [["净利润", "1"]],
        ["项目", "2025年前三季度(1-9月)"],
        section="二、财务报表",
        doc_id="601127_2025_三季报",
        kind_hint="is",
    )
    assert infer_unit([summary, income]) == "元"
