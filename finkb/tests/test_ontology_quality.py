"""本体质量门禁（P1-1）单测：结构指标与 OoPS 式常见坑。

真实库断言「当前数据是干净的」；其余用内存 DuckDB 构造坏样本，
确保每类问题都能被检出（不是只写了个报告）。
"""

from __future__ import annotations

import duckdb
import pytest

from finkb.ontology import quality
from finkb.fact_engine.selector import FINKB_ROOT

FACT_DB = FINKB_ROOT / "data" / "facts.duckdb"

_DDL = [
    "CREATE TABLE concepts(concept_id VARCHAR, label VARCHAR, layer VARCHAR, domain VARCHAR, "
    "parent_id VARCHAR, metric_id VARCHAR, definition VARCHAR)",
    "CREATE TABLE concept_relations(src_concept VARCHAR, rel_type VARCHAR, dst_concept VARCHAR)",
    "CREATE TABLE ontology_classes(class_id VARCHAR)",
    "CREATE TABLE ontology_object_properties(property_id VARCHAR, domain_class VARCHAR, "
    "range_class VARCHAR, inverse_of VARCHAR)",
    "CREATE TABLE ontology_defined_classes(class_id VARCHAR)",
    "CREATE TABLE inferred_concepts(company VARCHAR, period VARCHAR, class_id VARCHAR)",
    "CREATE TABLE facts_dedup(metric_id VARCHAR)",
]


def _db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for sql in _DDL:
        con.execute(sql)
    return con


def _checks(report: dict) -> dict[str, dict]:
    return {item["id"]: item for item in report["checks"]}


def test_real_database_is_clean():
    if not FACT_DB.exists():
        pytest.skip("facts.duckdb 不存在")
    report = quality.audit()
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["counts"]["concepts"] > 0


def test_duplicate_labels_are_reported():
    con = _db()
    con.execute("INSERT INTO concepts VALUES ('a:1','营业收入','L2','finance',NULL,NULL,'x'),"
                "('a:2',' 营业收入 ','L2','finance',NULL,NULL,'x')")
    report = quality._audit(con)
    assert _checks(report)["duplicate_labels"]["count"] == 1


def test_hierarchy_cycle_is_error():
    con = _db()
    con.execute("INSERT INTO concepts VALUES ('a:A','A','L1','meta','a:B',NULL,'x'),"
                "('a:B','B','L1','meta','a:A',NULL,'x')")
    report = quality._audit(con)
    assert "hierarchy_cycles" in report["errors"]


def test_dangling_relation_is_error():
    con = _db()
    con.execute("INSERT INTO concepts VALUES ('a:A','A','L1','meta',NULL,NULL,'x')")
    con.execute("INSERT INTO concept_relations VALUES ('a:A','is_a','a:missing')")
    report = quality._audit(con)
    assert "dangling_references" in report["errors"]


def test_inverse_mismatch_is_error():
    con = _db()
    con.execute("INSERT INTO concepts VALUES ('a:A','A','L1','meta',NULL,NULL,'x')")
    con.execute("INSERT INTO ontology_object_properties VALUES ('p:one','c','c','p:two'),"
                "('p:two','c','c',NULL)")
    report = quality._audit(con)
    assert "inverse_mismatch" in report["errors"]


def test_orphan_and_unmapped_are_warnings():
    con = _db()
    con.execute("INSERT INTO concepts VALUES ('a:A','A','L2','finance',NULL,'rev','x'),"
                "('a:B','B','L2','finance',NULL,'ghost','x')")
    con.execute("INSERT INTO facts_dedup VALUES ('rev')")
    report = quality._audit(con)
    checks = _checks(report)
    assert checks["orphan_concepts"]["count"] == 2
    assert checks["unmapped_metrics"]["count"] == 1
    assert report["ok"] is True
