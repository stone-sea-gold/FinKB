"""关系表表头族（relation_headers.yaml + relations 重写）单测。"""

from __future__ import annotations

from finkb.fact_engine import relations
from finkb.fact_engine.selector import SourceTable


def _table(header, rows, section="", page=7, doc="000001_2025_年报"):
    return SourceTable(
        doc_id=doc,
        company="000001",
        company_name="示例公司",
        period="2025",
        report_date="2026-03-01",
        logical_id="P1_T1",
        page=page,
        section=section,
        header=header,
        rows=rows,
    )


def _parse(table):
    kind, spec = relations.classify(table)
    return kind, relations.parse_table(table, kind, spec, "000001")


def test_classify_subsidiary_variant():
    assert relations.classify(_table(["子公司名称", "持股比例"], []))[0] == "subsidiary_of"


def test_classify_shareholder_variant():
    table = _table(["法人股东名称", "持股比例(%)", "股东性质"], [["甲公司", "5.00%", "境内非国有法人"]])
    assert relations.classify(table)[0] == "shareholder_of"
    _kind, edges = _parse(table)
    assert edges[0]["dst_id"] == "甲公司"
    assert edges[0]["attrs"]["ratio"] == 5.0


def test_classify_guarantees_variant():
    assert relations.classify(_table(["被担保方", "担保金额"], []))[0] == "guarantees_for"


def test_related_party_name_not_first_column():
    table = _table(["项目名称", "关联方", "期末账面余额"], [["应收账款", "甲公司", "100"]])
    kind, edges = _parse(table)
    assert kind == "related_party_of"
    assert edges[0]["dst_id"] == "甲公司"
    assert edges[0]["attrs"]["amount"] == "100"


def test_exclude_director_pay_table():
    table = _table(["姓名", "职务", "是否在公司关联方获取报酬"], [])
    assert relations.classify(table) is None


def test_skip_total_rows_and_keep_provenance():
    table = _table(
        ["子公司名称", "持股比例", "直接", "间接"],
        [["合计", "", "", ""], ["乙公司", "60.00%", "60.00", "0"]],
    )
    _kind, edges = _parse(table)
    assert len(edges) == 1
    edge = edges[0]
    assert edge["dst_id"] == "乙公司"
    assert edge["period"] == "2025"
    assert edge["page_num"] == 7
    assert edge["attrs"] == {"direct": 60.0, "indirect": 0.0, "ratio": 60.0}


def test_legacy_headerless_shareholder():
    table = _table([], [["甲公司", "境内非国有法人", "5.00%"]], section="前十名股东持股情况")
    kind, edges = _parse(table)
    assert kind == "shareholder_of"
    assert edges[0]["dst_id"] == "甲公司"
    assert edges[0]["attrs"]["ratio"] == 5.0
