"""闭世界数据校验（P1-2）单测：约束求值、派生比率、实体回填、附注覆盖。"""

from __future__ import annotations

import duckdb
import pytest

from finkb.fact_engine import data_check
from finkb.fact_engine.selector import FINKB_ROOT

FACT_DB = FINKB_ROOT / "data" / "facts.duckdb"

_DDL = [
    "CREATE TABLE concepts(concept_id VARCHAR, label VARCHAR, layer VARCHAR, metric_id VARCHAR, formula VARCHAR)",
    "CREATE TABLE ontology_constraints(constraint_id VARCHAR, concept_id VARCHAR, rule_type VARCHAR, "
    "expression VARCHAR, tolerance DOUBLE, severity VARCHAR)",
    "CREATE TABLE facts_dedup(company VARCHAR, period VARCHAR, metric_id VARCHAR, value_norm DOUBLE, norm_unit VARCHAR)",
    "CREATE TABLE relations(src_entity_id VARCHAR, dst_entity_id VARCHAR)",
    "CREATE TABLE note_anchors(doc_id VARCHAR, metric_id VARCHAR, anchor_type VARCHAR)",
    "CREATE TABLE conflicts(id VARCHAR)",
]


def _db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for sql in _DDL:
        con.execute(sql)
    con.executemany(
        "INSERT INTO ontology_constraints VALUES (?,?,?,?,?,?)",
        [
            ("acct.eq", "fibo:Asset", "acct_equation",
             "total_assets == total_liabilities + total_equity", None, "error"),
            ("range.gm", "self:gross_margin", "range", "-1 <= gross_margin <= 1", None, "warn"),
            ("period.income", "fibo:Income", "required_period",
             "period IS NOT NULL AND period <> ''", None, "error"),
            ("unit.eps", "xbrli:perShareItem", "unit_check", "norm_unit == '元/股'", None, "error"),
        ],
    )
    con.execute("INSERT INTO concepts VALUES ('self:gross_margin','毛利率','L3',NULL,'(revenue - operating_cost) / revenue'),"
                "('xbrli:perShareItem','每股','L1','eps_basic',NULL)")
    return con


def _checks(report: dict) -> dict[str, dict]:
    return {item["id"]: item for item in report["checks"]}


def test_real_database_ok():
    if not FACT_DB.exists():
        pytest.skip("facts.duckdb 不存在")
    report = data_check.audit()
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["counts"]["constraints"] == 10


def test_accounting_equation_violation_is_error():
    con = _db()
    con.execute("INSERT INTO facts_dedup VALUES ('000002','2025','total_assets',100,'亿元'),"
                "('000002','2025','total_liabilities',60,'亿元'),"
                "('000002','2025','total_equity',30,'亿元')")
    report = data_check._audit(con)
    assert "acct.eq" in report["errors"]


def test_accounting_equation_passes_within_tolerance():
    con = _db()
    con.execute("INSERT INTO facts_dedup VALUES ('000002','2025','total_assets',100,'亿元'),"
                "('000002','2025','total_liabilities',60.01,'亿元'),"
                "('000002','2025','total_equity',39.99,'亿元')")
    report = data_check._audit(con)
    assert "acct.eq" not in report["errors"]


def test_derived_ratio_is_computed_and_flagged_as_warning():
    con = _db()
    con.execute("INSERT INTO facts_dedup VALUES ('000333','2025','revenue',100,'亿元'),"
                "('000333','2025','operating_cost',250,'亿元')")
    report = data_check._audit(con)
    check = _checks(report)["range.gm"]
    assert check["count"] == 1 and check["severity"] == "warn"
    assert report["ok"] is True


def test_required_period_and_unit_checks():
    con = _db()
    con.execute("INSERT INTO facts_dedup VALUES ('000002','','revenue',1,'亿元')")
    con.execute("INSERT INTO facts_dedup VALUES ('000002','2025','eps_basic',1,'元')")
    report = data_check._audit(con)
    assert "period.income" in report["errors"]
    assert "unit.eps" in report["errors"]


def test_entity_annotation_must_be_complete():
    con = _db()
    con.execute("INSERT INTO relations VALUES ('co:000002', NULL)")
    report = data_check._audit(con)
    assert "entity_annotation" in report["errors"]


def test_note_coverage_warning_uses_note_eligible_denominator():
    con = _db()
    con.executemany("INSERT INTO note_anchors VALUES (?, 'revenue', 'note')",
                    [[f"doc{i}"] for i in range(20)])
    report = data_check._audit(con)
    check = _checks(report)["note_anchor_coverage"]
    assert check["count"] == 12  # 13 个核心科目里只有 revenue 有锚
    assert check["severity"] == "warn"
