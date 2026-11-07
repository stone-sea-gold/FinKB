"""P7 跨期对比单测：多期间并排、期间归一、缺失行、拒答与 MCP 工具。

全部离线，不连网络。
"""

from __future__ import annotations

import pytest

from finkb.qa import pipeline
from finkb.qa.compare import canonical_period, compare, question_periods
from finkb.qa import mcp_server

pytestmark = pytest.mark.skipif(
    not pipeline.FACT_DB.exists(), reason="需要开发语料 data/facts.duckdb（仓库不含数据）"
)

Q3 = "美的集团 2023-2025 年营业收入"


def test_canonical_period_variants():
    assert canonical_period("2024") == "2024"
    assert canonical_period("2024年") == "2024"
    assert canonical_period("2024H1") == "2024H1"
    assert canonical_period("2024上半年") == "2024H1"
    assert canonical_period("2024Q1") == "2024Q1"
    assert canonical_period("2024前三季度") == "2024Q1-Q3"
    assert canonical_period("营业收入") is None


def test_question_periods_expands_range():
    assert question_periods("美的集团 2023-2025 年营业收入") == ["2023", "2024", "2025"]
    assert question_periods("比亚迪 2024H1 和 2024 年营业收入") == ["2024H1", "2024"]
    assert question_periods("美的集团营业收入") == []


def test_compare_three_periods():
    payload = compare(Q3)
    assert payload["refused"] is False
    assert payload["tier"] == "metric"
    rows = payload["data"]["rows"]
    assert [row["period"] for row in rows] == ["2023", "2024", "2025"]
    assert all(row["value"] is not None for row in rows)
    assert len(payload["ledger"]) == 3
    assert len(payload["citations"]) == 3
    assert "2023" in payload["text"] and "2025" in payload["text"]
    assert payload["schema_version"] and payload["disclaimer"]


def test_compare_explicit_periods_are_canonicalized():
    payload = compare("美的集团营业收入", periods=["2023年", "2024", "2024H1"])
    assert payload["refused"] is False
    assert payload["data"]["periods"] == ["2023", "2024", "2024H1"]
    assert payload["data"]["rows"][2]["value"] is None  # 000333 无 2024H1 营收
    assert len(payload["ledger"]) == 2


def test_compare_single_period_refused():
    payload = compare("美的集团 2025 年营业收入")
    assert payload["refused"] is True
    assert "至少需要两个期间" in payload["text"]
    assert payload["data"] == {}


def test_compare_missing_metric_refused():
    payload = compare("美的集团 2023-2025 年")
    assert payload["refused"] is True
    assert "明确一个指标和公司" in payload["text"]


def test_mcp_compare_tool():
    payload = mcp_server.call_tool("compare", {"question": Q3})
    assert payload["refused"] is False
    assert [row["period"] for row in payload["data"]["rows"]] == ["2023", "2024", "2025"]
    assert payload["data"]["company"] == "000333"
