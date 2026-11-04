"""关系抽取与冲突检测的单测。"""

from __future__ import annotations

import json

from finkb.fact_engine import conflicts, relations
from finkb.fact_engine.selector import SourceTable
from finkb.fact_engine.store import FactStore


def _table(header, rows, section="", page=1, doc="000001_2025_年报"):
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


def _spec(kind: str) -> dict:
    families = relations.load_header_config()["families"]
    return next(f for f in families if f["rel_type"] == kind)


def test_classify_four_kinds():
    assert relations.classify(_table(["子公司全称", "持股比例"], []))[0] == "subsidiary_of"
    assert relations.classify(_table(["公司名称", "关联方关系"], []))[0] == "related_party_of"
    assert relations.classify(_table(["担保对象名称", "实际担保金额"], []))[0] == "guarantees_for"
    assert relations.classify(_table(["股东名称", "持股比例"], []))[0] == "shareholder_of"
    assert relations.classify(_table(["营业收入", "营业成本"], [])) is None


def test_parse_subsidiary_and_shareholder():
    table = _table(
        ["子公司全称", "主要经营地", "注册地", "业务性质", "注册资本", "直接", "间接"],
        [["甲公司", "深圳", "深圳", "制造", "1 亿", "80.00", "0.00"], ["合计", "", "", "", "", "", ""]],
    )
    edges = relations.parse_table(table, "subsidiary_of", _spec("subsidiary_of"), "000001")
    assert len(edges) == 1
    assert edges[0]["dst_id"] == "甲公司"
    assert edges[0]["attrs"] == {"direct": 80.0, "indirect": 0.0}

    holder = _table(["股东名称", "股东类型", "持股比例"], [["张三", "境内自然人", "5.20%"]])
    edges = relations.parse_table(holder, "shareholder_of", _spec("shareholder_of"), "000001")
    assert edges[0]["dst_type"] == "person"
    assert edges[0]["attrs"]["ratio"] == 5.2


def test_write_relations_end_to_end(tmp_path):
    doc = tmp_path / "000001_2025_年报"
    doc.mkdir()
    (doc / "report.json").write_text(
        json.dumps(
            {
                "pages": 3,
                "tables": [
                    {
                        "parent_id": "P1_T1",
                        "page_start": 1,
                        "section": "主要子公司",
                        "rows": [["甲公司", "深圳", "深圳", "制造", "1 亿", "80.00", "0.00"]],
                    }
                ],
                "logical_tables": [
                    {
                        "parent_id": "P1_T1",
                        "logical_id": "P1_T1#S01",
                        "row_range": [0, 1],
                        "header": ["子公司全称", "主要经营地", "注册地", "业务性质", "注册资本", "直接", "间接"],
                        "section_path": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with FactStore(":memory:") as store:
        assert relations.write_relations(store, doc, {}) == 1
        assert relations.write_relations(store, doc, {}) == 1  # 覆盖写，不重复
        rows = store.conn.execute("SELECT rel_type, dst_id FROM relations").fetchall()
    assert rows == [("subsidiary_of", "甲公司")]


def _insert(store, value, doc):
    return store.insert_fact(
        company="000001",
        period="2025Q1",
        metric_id="eps_basic",
        unit_family="ratio",
        unit_of_account="元/股",
        norm_unit="元/股",
        value_raw=value,
        raw_text_matched=str(value),
        value_norm=value,
        caliber="合并报表",
        reporting_party="000001",
        report_date="2026-04-30",
        source_doc=doc,
        page_num=1,
        table_id="P1_T1",
        match_level="exact",
    )


def test_detect_conflicts():
    with FactStore(":memory:") as store:
        _insert(store, 3.12, "000001_2025_一季报")
        _insert(store, 1.0391, "000001_2026_一季报")
        assert conflicts.detect_conflicts(store) == 1
        row = store.conn.execute("SELECT value_a, value_b, doc_a, doc_b FROM conflicts").fetchone()
        assert row == (3.12, 1.0391, "000001_2025_一季报", "000001_2026_一季报")

    with FactStore(":memory:") as store:
        _insert(store, 3.12, "000001_2025_一季报")
        _insert(store, 3.12, "000001_2026_一季报")
        assert conflicts.detect_conflicts(store) == 0
