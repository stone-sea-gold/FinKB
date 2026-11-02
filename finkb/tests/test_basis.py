"""ADR-055：率值的期间口径标注。"""

from __future__ import annotations

import pytest

from finkb.fact_engine.basis import annotate_period_basis
from finkb.fact_engine.store import FactStore


def _insert(store: FactStore, metric_id: str, value: float, period: str) -> None:
    store.insert_fact(
        company="600036",
        period=period,
        metric_id=metric_id,
        unit_family="元",
        unit_of_account="百万元",
        norm_unit="亿元",
        value_raw=value,
        raw_text_matched=str(value),
        value_norm=value,
        caliber="consolidated",
        reporting_party="本行",
        report_date="2025-06-30",
        source_doc="doc",
        page_num=1,
        table_id="t",
        match_level="alias",
    )


def test_half_year_roe_is_marked_annualized(tmp_path) -> None:
    """招行 2025H1：披露 13.85，净利 749 / 净资产 12350 ≈ 6.06%，13.85 是年化值。"""
    with FactStore(tmp_path / "facts.duckdb") as store:
        _insert(store, "net_profit", 749.0, "2025H1")
        _insert(store, "net_assets", 12350.0, "2025H1")
        _insert(store, "roe_weighted", 13.85, "2025H1")
        assert annotate_period_basis(store)["annualized"] == 1
        got = store.conn.execute(
            "SELECT period_basis FROM facts WHERE metric_id = 'roe_weighted'"
        ).fetchone()[0]
        assert got == "annualized"


def test_unannualized_and_annual_are_marked_period(tmp_path) -> None:
    with FactStore(tmp_path / "facts.duckdb") as store:
        # 半年报但不年化（6.06 ≈ 749/12350），应当判 period
        _insert(store, "net_profit", 749.0, "2025H1")
        _insert(store, "net_assets", 12350.0, "2025H1")
        _insert(store, "roe_weighted", 6.06, "2025H1")
        # 年报没有年化问题
        _insert(store, "net_profit", 749.0, "2025")
        _insert(store, "net_assets", 12350.0, "2025")
        _insert(store, "roe_weighted", 6.06, "2025")
        counts = annotate_period_basis(store)
        assert counts == {"annualized": 0, "period": 2}
