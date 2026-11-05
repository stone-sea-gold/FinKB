"""指标治理 v1 的单测（ADR-060）：配置结构、SQL 只读、参数、pending。全部离线。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from finkb.fact_engine import metric_governance


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


BASE_DICT = {
    "version": 1,
    "metrics": [
        {
            "metric_id": "revenue",
            "metric_name": "营业总收入",
            "unit_default": "亿元",
            "category": "盈利",
            "aliases": ["营业收入"],
        }
    ],
}


def test_sql_safety():
    assert metric_governance.check_sql_safety("SELECT 1") == []
    assert metric_governance.check_sql_safety("WITH x AS (SELECT 1) SELECT * FROM x") == []
    assert metric_governance.check_sql_safety("DELETE FROM facts") != []
    assert metric_governance.check_sql_safety("SELECT 1; DROP TABLE facts") != []
    assert metric_governance.check_sql_safety("ATTACH 'x.duckdb'") != []


def test_real_config_is_clean():
    result = metric_governance.audit(check_db=False)
    assert result.ok, result.hard_errors
    assert result.governed["raw"] == 36
    assert result.governed["query"] == 5
    assert result.governed["pending"] == 2
    assert any("pending.expense_ratio" in item for item in result.warnings)


def test_missing_ref_is_hard(tmp_path):
    metrics = _write(tmp_path, "metrics.yaml", {
        "version": 1,
        "fact_sql": "SELECT 1",
        "metrics": {"debt_ratio": {"name": "资产负债率", "unit": "%", "kind": "query", "ref": "nope"}},
    })
    result = metric_governance.audit(
        db_path=None, metrics_path=metrics, queries_path=_write(tmp_path, "q.yaml", {"queries": {}}),
        dict_path=_write(tmp_path, "d.yaml", BASE_DICT), ontology_path=tmp_path / "missing.yaml",
        check_db=False,
    )
    assert not result.ok
    assert any("不在 wiki_queries.yaml" in item for item in result.hard_errors)


def test_dangerous_sql_is_hard(tmp_path):
    queries = _write(tmp_path, "q.yaml", {
        "queries": {"bad": {"params": ["company"], "sql": "DELETE FROM facts WHERE company = ?"}},
    })
    metrics = _write(tmp_path, "metrics.yaml", {
        "version": 1,
        "metrics": {"debt_ratio": {"name": "资产负债率", "unit": "%", "kind": "query", "ref": "bad"}},
    })
    result = metric_governance.audit(
        db_path=None, metrics_path=metrics, queries_path=queries,
        dict_path=_write(tmp_path, "d.yaml", BASE_DICT), ontology_path=tmp_path / "missing.yaml",
        check_db=False,
    )
    assert not result.ok
    assert any("必须以 SELECT" in item or "危险关键字" in item for item in result.hard_errors)


def test_param_count_and_names(tmp_path):
    queries = _write(tmp_path, "q.yaml", {
        "queries": {"q": {"params": ["company", "period"], "sql": "SELECT ?"}},
    })
    metrics = _write(tmp_path, "metrics.yaml", {
        "version": 1,
        "fact_sql": "SELECT 1",
        "metrics": {"debt_ratio": {"name": "资产负债率", "unit": "%", "kind": "query", "ref": "q"}},
    })
    result = metric_governance.audit(
        db_path=None, metrics_path=metrics, queries_path=queries,
        dict_path=_write(tmp_path, "d.yaml", BASE_DICT), ontology_path=tmp_path / "missing.yaml",
        check_db=False,
    )
    assert not result.ok
    assert any("参数个数" in item for item in result.hard_errors)

    queries_bad = _write(tmp_path, "q2.yaml", {
        "queries": {"q": {"params": ["bucket"], "sql": "SELECT ?"}},
    })
    result = metric_governance.audit(
        db_path=None, metrics_path=metrics, queries_path=queries_bad,
        dict_path=_write(tmp_path, "d.yaml", BASE_DICT), ontology_path=tmp_path / "missing.yaml",
        check_db=False,
    )
    assert any("未知参数名" in item for item in result.hard_errors)


def test_ontology_concept_must_exist(tmp_path):
    ontology = _write(tmp_path, "ontology.yaml", {"concepts": [{"concept_id": "self:debt_ratio"}]})
    queries = _write(tmp_path, "q.yaml", {
        "queries": {"q": {"params": ["company"], "sql": "SELECT ?", "unit": "%"}},
    })
    metrics = _write(tmp_path, "metrics.yaml", {
        "version": 1,
        "fact_sql": "SELECT 1",
        "metrics": {
            "debt_ratio": {
                "name": "资产负债率", "unit": "%", "kind": "query", "ref": "q",
                "ontology_concepts": ["self:missing"],
            }
        },
    })
    result = metric_governance.audit(
        db_path=None, metrics_path=metrics, queries_path=queries,
        dict_path=_write(tmp_path, "d.yaml", BASE_DICT), ontology_path=ontology,
        check_db=False,
    )
    assert any("不在 ontology.yaml" in item for item in result.hard_errors)
