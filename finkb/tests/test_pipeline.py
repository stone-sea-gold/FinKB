"""三档契约（ADR-060）单测：档位顺序、能力剪枝、残差门、trace、强制档位。

全部离线；不连网络的 embedding，只读本地 DuckDB/切块索引。
"""

from __future__ import annotations

import pytest

from finkb.qa import pipeline
from finkb.qa.fact_lane import FACT_DB
from finkb.qa.synth import FakeBackend

TEXT_Q = "比亚迪 2025 年报里，公司如何描述海外市场的区域布局？"

pytestmark = pytest.mark.skipif(
    not FACT_DB.exists(), reason="需要开发语料 data/facts.duckdb（仓库不含数据）"
)


def _steps(payload: dict) -> list[dict]:
    return payload.get("trace", [])


def _has_step(payload: dict, step: str) -> bool:
    return any(item.get("step") == step for item in _steps(payload))


def test_residual_qualifiers():
    assert pipeline.residual_qualifiers("万科A 2025 年的营业收入是多少亿元？") == []
    assert "海外" in pipeline.residual_qualifiers("比亚迪 2025 年海外收入是多少亿元？")
    assert "分产品" in pipeline.residual_qualifiers("美的集团 2025 年分产品的营业收入是多少？")


def test_tiers_are_explicit():
    fact = pipeline.ask("万科A（000002）2025 年的营业收入是多少亿元？")
    assert fact["tier"] == "metric" and fact["route"] == "fact" and fact["confidence"] > 0.9
    ontology = pipeline.ask("哪些公司 2025 年的资产负债率超过 70%？")
    assert ontology["tier"] == "ontology" and ontology["route"] == "ontology"
    text = pipeline.ask("比亚迪 2025 年报里，公司如何描述海外市场的区域布局？")
    assert text["tier"] == "document" and text["route"] == "text"


def test_residual_gate_downgrades():
    payload = pipeline.ask("比亚迪 2025 年分产品的营业收入是多少亿元？")
    assert _has_step(payload, "residual_gate")
    assert payload["tier"] == "document"


def test_forced_tier():
    payload = pipeline.ask("比亚迪 2025 年报里，公司如何描述海外市场的区域布局？", tier="document")
    assert payload["tier"] == "document"


def test_capability_pruning_skips_metric():
    caps = {"metric": False, "ontology": True, "document": True}
    payload = pipeline.ask("万科A 2025 年的营业收入是多少亿元？", capabilities_override=caps)
    assert payload["tier"] != "metric"
    assert any(item.get("step") == "prune" and item.get("tier") == "metric" for item in _steps(payload))


def test_tier_unavailable_refuses():
    caps = {"metric": False, "ontology": False, "document": False}
    payload = pipeline.ask("万科A 2025 年的营业收入是多少亿元？", tier="metric", capabilities_override=caps)
    assert payload["refused"] is True and payload["confidence"] == 0.0
    assert any(item.get("reason") == "tier-unavailable" for item in _steps(payload))


def test_all_capability_missing_refuses():
    caps = {"metric": False, "ontology": False, "document": False}
    payload = pipeline.ask("比亚迪 2025 年报里，公司如何描述海外市场的区域布局？", capabilities_override=caps)
    assert payload["refused"] is True
    assert any(item.get("reason") == "no-capability" for item in _steps(payload))


def test_precheck_refusal_is_outside_tiers():
    payload = pipeline.ask("比亚迪 2025 年的港股股价是多少？")
    assert payload["refused"] is True and payload["confidence"] == 0.0


def test_trace_contains_capability_and_hint():
    payload = pipeline.ask("万科A 2025 年的营业收入是多少亿元？")
    steps = {item["step"] for item in _steps(payload)}
    assert {"capability", "router_hint", "attempt"} <= steps


def test_synthesis_document_with_backend():
    env = pipeline.ask(TEXT_Q, synthesize=False)
    assert env["tier"] == "document" and env["citations"]
    ref = env["citations"][0]
    answer = f"公司持续推进海外布局，海外业务覆盖多个国家。{ref}"
    backend = FakeBackend([{"answer": answer, "used_ids": [], "refused": False, "missing": [], "notes": []}])
    payload = pipeline.ask(TEXT_Q, synthesize=backend)
    assert payload["synthesis"]["used"] is True
    assert payload["synthesis"]["backend"] == "fake"
    assert payload["text"] == answer
    assert payload["citations"] == [ref]
    assert all(payload["synthesis"]["checks"].values())


def test_synthesis_falls_back_when_verification_fails():
    backend = FakeBackend([{"answer": "公司持续推进海外布局。[Ref: 不存在_2025_年报#P1]", "used_ids": [], "refused": False, "missing": [], "notes": []}])
    payload = pipeline.ask(TEXT_Q, synthesize=backend)
    assert payload["synthesis"]["used"] is False
    assert payload["synthesis"]["reason"] == "verify_failed"
    assert payload["synthesis"]["attempts"] == 2
    assert "第" in payload["text"]  # 回退到抽取式答案


def test_synthesis_disabled_marks_no_backend():
    payload = pipeline.ask(TEXT_Q, synthesize=False)
    assert payload["synthesis"]["used"] is False
    assert payload["synthesis"]["reason"] == "no_backend"


def test_synthesis_refused_becomes_refusal():
    backend = FakeBackend([{"answer": "", "used_ids": [], "refused": True, "missing": ["海外分区域数据"], "notes": []}])
    payload = pipeline.ask(TEXT_Q, synthesize=backend)
    assert payload["refused"] is True
    assert payload["text"].startswith("证据不足：")


def test_metric_tier_never_synthesizes():
    backend = FakeBackend([])
    payload = pipeline.ask("万科A（000002）2025 年的营业收入是多少亿元？", synthesize=backend)
    assert payload["tier"] == "metric"
    assert "synthesis" not in payload
