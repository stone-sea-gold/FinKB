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


def test_column_periods_year_end_columns_keep_their_year():
    # 「2023年末」不能被当成当年期末；拆表后这类表头才露出来
    assert column_periods(["", "2025年末", "2024年末", "2023年末"], "2025", "bs") == {
        1: "2025",
        2: "2024",
        3: "2023",
    }


def test_column_periods_prior_year_cumulative_label():
    # 601127 三季报利润表：表头带年份但写「前三季度」，不能一律当成当年
    assert column_periods(["项目", "2025年前三季度(1-9月)", "2024年前三季度(1-9月)"], "2025Q3", "is") == {
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


def test_missing_columns_flags_silent_gap():
    from finkb.fact_engine.extractor import missing_columns

    table = make_table(
        [["营业收入", "100", "90"], ["净利润", "10", "9"]],
        ["项目", "2025年", "2024年"],
        section="主要会计数据",
    )
    periods = column_periods(table.header, "2025", "summary")
    assert missing_columns(table, periods, [{"col": 1, "metric_id": "revenue"}]) == [2]
    assert missing_columns(table, periods, [{"col": 1}, {"col": 2}]) == []


def test_repair_reads_cell_when_model_mistypes():
    from finkb.fact_engine.verifier import repair

    table = make_table(
        [["营业收入", "1,234.5", "1,000.0"], ["营业成本", "-", "800.0"]],
        ["项目", "2025年", "2024年"],
        section="主要会计数据",
    )
    periods = column_periods(table.header, "2025", "summary")
    metrics = {"revenue": REVENUE}
    assert verify({"metric_id": "revenue", "row": 0, "col": 1, "value_raw": "1,234.0"}, table, metrics, periods, "元")[0] is None
    fixed = repair({"metric_id": "revenue", "row": 0, "col": 1, "value_raw": "1,234.0"}, table, metrics, periods, "元")
    assert fixed is not None and fixed.value_raw == 1234.5 and fixed.match_level == "repaired"
    # 行标签对不上（把营业成本当营业收入）不修
    assert repair({"metric_id": "revenue", "row": 1, "col": 1}, table, metrics, periods, "元") is None


def test_split_header_blocks_splits_embedded_header():
    """银行「经营业绩 + 规模指标」拼表：第二段表头要把期间换掉。"""
    from finkb.fact_engine.selector import SourceTable, split_header_blocks

    table = SourceTable(
        doc_id="x_2025_半年报",
        company="x",
        company_name="X",
        period="2025H1",
        report_date="2025-08-31",
        logical_id="x#P7_T01",
        page=7,
        section="主要会计数据",
        header=["(人民币百万元)", "2025年1-6月", "2024年1-6月"],
        rows=[
            ["营业收入", "169,969", "172,945"],
            ["(人民币百万元)", "2025年6月30日", "2024年12月31日"],
            ["总资产", "12,657,151", "12,152,036"],
        ],
    )
    blocks = split_header_blocks(table)
    assert len(blocks) == 2
    assert blocks[0].header[1] == "2025年1-6月"
    assert blocks[1].header[1] == "2025年6月30日"
    assert blocks[1].rows == [["总资产", "12,657,151", "12,152,036"]]


def test_normalize_item_accepts_metric_key():
    from finkb.fact_engine.extractor import _normalize_item

    item = _normalize_item({"metric": "cash", "row": 0, "col": 1, "value_raw": "1"})
    assert item["metric_id"] == "cash" and "metric" not in item


def test_unit_from_labels_ignores_per_share():
    from finkb.fact_engine.ingest import unit_from_labels

    assert unit_from_labels([["营业收入(千元)", "456,451,731"]]) == "千元"
    assert unit_from_labels([["营业收入（万元）", "1"]]) == "万元"
    assert unit_from_labels([["基本每股收益(元)", "5.8"]]) is None
    assert unit_from_labels([["营业收入", "1"]]) is None


def test_cf_select_skips_parent_and_notes():
    merged = make_table(
        [["经营活动产生/(使用)的现金流量净额", "5"]] + [[f"现金流{i}", "1"] for i in range(30)],
        ["项目", "2025年度"],
        section="合并现金流量表",
        kind_hint="cf_merged",
    )
    parent = make_table(
        [["经营活动产生的现金流量净额", "5"]] + [[f"现金流{i}", "1"] for i in range(32)],
        ["项目", "2025年度"],
        section="母公司现金流量表",
        kind_hint="cf_parent",
    )
    notes = make_table(
        [["经营活动产生的现金流量净额", "5"]] + [[f"现金流{i}", "1"] for i in range(40)],
        ["项目", "2025年度"],
        section="(64) 现金流量表项目附注",
        kind_hint="cf_notes",
    )
    assert merged.kind == "cf"  # 斜杠写法也要认
    picked = select_tables([parent, notes, merged])
    assert [t.logical_id for t in picked] == ["cf_merged"]


def test_expense_metric_stored_positive(tmp_path):
    from finkb.fact_engine.ingest import write_document
    from finkb.fact_engine.store import FactStore

    cost = Metric(metric_id="operating_cost", metric_name="营业成本", unit_default="亿元", category="盈利", aliases=[])
    table = make_table([["减:营业成本", "(100)"]], ["项目", "2025年"], section="合并利润表")
    store = FactStore(tmp_path / "t.duckdb")
    stats = write_document(store, [(table, [{"metric_id": "operating_cost", "row": 0, "col": 1, "value_raw": "(100)"}], {1: "2025"})], [cost], "元")
    assert stats["facts"] == 1
    assert store.conn.execute("select value_raw, value_norm from facts").fetchone() == (100.0, 1e-06)


def _page_table(rows, section, page, header=("项目", "2025年"), tid=None):
    from dataclasses import replace

    t = make_table(rows, list(header), section=section, kind_hint=tid or f"t{page}")
    return replace(t, page=page)


def test_bs_split_pages_merged_by_content():
    """资产负债表被切成「资产/负债/权益」三页、页标题只有日期，也要拼成整张。"""
    filler = [[f"资产项目{i}", "1"] for i in range(8)]
    p1 = _page_table([["资产", "1"], ["货币资金", "2"], ["流动资产合计", "3"]] + filler, "2025 年 12 月 31 日", 10, tid="bs1")
    p2 = _page_table([["非流动资产", "4"]] + filler + [["资产总计", "5"]], "中信证券股份有限公司", 11, tid="bs2")
    p3 = _page_table([["负债", "6"]] + filler + [["负债合计", "7"]], "中信证券股份有限公司", 12, tid="bs3")
    p4 = _page_table([["股东权益", "8"], ["归属于母公司股东权益合计", "9"], ["少数股东权益", "10"], ["负债和股东权益总计", "11"]], "2025 年 12 月 31 日", 13, tid="bs4")
    picked = select_tables([p1, p2, p3, p4])
    assert [t.logical_id for t in picked] == ["bs1", "bs2", "bs3", "bs4"]


def test_cf_truncated_page_gets_continuation():
    """现金流量表首页只到「经营活动净额」，投资活动在下一页，也要接上。"""
    first = _page_table(
        [["销售商品、提供劳务收到的现金", "1"]] + [[f"经营{i}", "1"] for i in range(9)] + [["经营活动产生的现金流量净额", "5"]],
        "合并现金流量表", 20, tid="cf1")
    second = _page_table(
        [["收回投资收到的现金", "1"]] + [[f"投资{i}", "1"] for i in range(9)] + [["投资活动产生的现金流量净额", "2"]],
        "合并现金流量表(续)", 21, tid="cf2")
    picked = select_tables([first, second])
    assert [t.logical_id for t in picked] == ["cf1", "cf2"]


def test_bs_missing_first_page_recovered():
    """首页被标成审计段落、从「非流动资产」开始，要向前补回流动资产页。"""
    filler = [[f"项目{i}", "1"] for i in range(8)]
    p0 = _page_table([["流动资产", "1"], ["货币资金", "2"], ["流动资产合计", "3"]] + filler, "六、注册会计师对财务报表审计的责任 (续)", 30, tid="bs0")
    p1 = _page_table([["非流动资产", "4"]] + filler + [["资产总计", "5"]], "合并资产负债表(续)", 31, tid="bs1")
    p2 = _page_table([["负债", "6"]] + filler + [["负债合计", "7"]], "合并资产负债表(续)", 32, tid="bs2")
    p3 = _page_table([["股东权益", "8"], ["少数股东权益", "9"], ["负债和股东权益总计", "10"]], "合并资产负债表(续)", 33, tid="bs3")
    picked = select_tables([p0, p1, p2, p3])
    assert [t.logical_id for t in picked] == ["bs0", "bs1", "bs2", "bs3"]


def test_page_unit_reads_chinese_note(tmp_path):
    from finkb.fact_engine.ingest import page_unit

    (tmp_path / "page_anchored.md").write_text(
        '<!-- META: {"doc": "x", "page": 8, "section": "主要会计数据"} -->\n单位：百万元\n',
        encoding="utf-8",
    )
    assert page_unit(tmp_path, 8) == "百万元"
    assert page_unit(tmp_path, 9) is None


def test_is_prefers_period_statement_over_five_year_summary():
    """五年摘要（2021–2025）不是当期法定利润表：有当期两列时优先当期表。"""
    filler = [[f"项目{i}", "1"] for i in range(22)]
    five_year = make_table(
        [["营业收入", "1", "2", "3", "4", "5"], ["净利润", "1", "2", "3", "4", "5"]] + filler,
        ["项目", "2021年", "2022年", "2023年", "2024年", "2025年"],
        section="合并利润表",
        kind_hint="is_5y",
    )
    current = make_table(
        [["营业收入", "1", "2"], ["净利润", "1", "2"]] + filler,
        ["项目", "2025年度", "2024年度"],
        section="合并利润表",
        kind_hint="is_cur",
    )
    assert five_year.kind == "is" and current.kind == "is"
    picked = select_tables([five_year, current])
    assert [t.logical_id for t in picked] == ["is_cur"]


def test_statement_unit_from_summary_ratio():
    """三表单位靠与 summary 的重叠科目反推，而不是页面正文的「单位：元」。"""
    from finkb.fact_engine.ingest import _infer_statement_unit

    def obs(value):
        return {"metric_id": "revenue", "value_raw": value}

    # summary 元、三表千元
    summary = make_table([["营业收入", "803,964,958,000"]], ["项目", "2025年"], section="主要会计数据", kind_hint="s1")
    stmt = make_table([["营业收入", "803,964,958"]], ["项目", "2025年"], kind_hint="b1")
    assert _infer_statement_unit([(summary, [obs(803964958000.0)], None), (stmt, [obs(883729883.0)], None)]) == "千元"

    # summary 万元、三表元
    summary2 = make_table([["营业收入（万元）", "22,282,423.85"]], ["项目", "2025年"], section="主要会计数据", kind_hint="s2")
    stmt2 = make_table([["营业收入", "222,824,238,000"]], ["项目", "2025年"], kind_hint="b2")
    assert _infer_statement_unit([(summary2, [obs(22282423.85)], None), (stmt2, [obs(222824238000.0)], None)]) == "元"
