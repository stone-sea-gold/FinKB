"""L1 仓储与指标字典的离线测试。"""

from __future__ import annotations

from datetime import date

import pytest

from finkb.fact_engine.metric_dict import (
    DEFAULT_METRIC_DICT,
    Metric,
    load_metric_dict,
)
from finkb.fact_engine.store import FactStore


def _fact(store: FactStore, **overrides) -> int:
    kwargs = dict(
        company="002594",
        period="2025",
        metric_id="revenue",
        unit_family="money",
        unit_of_account="元",
        norm_unit="亿元",
        value_raw=803_964_958_000.0,
        raw_text_matched="803,964,958,000.00",
        value_norm=8039.64958,
        caliber="合并报表",
        reporting_party="比亚迪股份有限公司",
        report_date=date(2026, 3, 27),
        source_doc="002594_2025_年报",
        page_num=11,
        table_id="002594_2025_年报#P11_T07",
        match_level="exact",
    )
    kwargs.update(overrides)
    return store.insert_fact(**kwargs)


def test_default_dict_loads():
    metrics = load_metric_dict()
    assert len(metrics) >= 15
    ids = [m.metric_id for m in metrics]
    assert len(ids) == len(set(ids))
    revenue = next(m for m in metrics if m.metric_id == "revenue")
    assert "营业收入" in revenue.aliases
    assert revenue.unit_family == "money"
    assert revenue.norm_unit == "亿元"


def test_unit_derivation_covers_all_metrics():
    for m in load_metric_dict():
        assert m.unit_family in {"money", "percent", "ratio", "count"}
        assert m.norm_unit


def test_unknown_unit_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "metrics:\n"
        "  - {metric_id: x, metric_name: X, unit_default: 万美元, category: 盈利, aliases: [x]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unit_default"):
        load_metric_dict(path)


def test_duplicate_id_rejected(tmp_path):
    path = tmp_path / "dup.yaml"
    path.write_text(
        "metrics:\n"
        "  - {metric_id: x, metric_name: X, unit_default: 亿元, category: 盈利, aliases: [x]}\n"
        "  - {metric_id: x, metric_name: Y, unit_default: 亿元, category: 盈利, aliases: [y]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复"):
        load_metric_dict(path)


def test_store_loads_metric_dict():
    with FactStore() as store:
        metrics = load_metric_dict(DEFAULT_METRIC_DICT)
        n = store.load_metric_dict(metrics)
        assert n == len(metrics)
        rows = store.metrics()
        assert len(rows) == n
        assert {r[0] for r in rows} >= {"revenue", "net_profit", "roe_weighted"}


def test_duplicate_source_doc_rejected():
    with FactStore() as store:
        _fact(store)
        with pytest.raises(Exception):
            _fact(store)  # 同一 (公司,期间,指标,口径,文档) 再来一条


def test_dedup_prefers_newer_report_date():
    """同一汇总指标在年报与半年报里各报一次，按公告日期取后者。"""
    with FactStore() as store:
        _fact(store, source_doc="002594_2025_年报", report_date=date(2026, 3, 27), value_norm=8039.65)
        _fact(store, source_doc="002594_2026_半年报", report_date=date(2026, 8, 20), value_norm=8040.1)
        rows = store.conn.execute(
            "SELECT source_doc, report_date FROM facts_dedup WHERE metric_id = 'revenue'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "002594_2026_半年报"


def test_unmatched_pool_rejects_bad_reason():
    with FactStore() as store:
        with pytest.raises(ValueError):
            store.add_unmatched({"metric_id": "revenue"}, "dropped")
        store.add_unmatched({"metric_id": "revenue"}, "literal_not_found")
        assert len(store.unmatched()) == 1
