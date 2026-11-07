"""多轮上下文契约（P5）单测：确定性槽位继承、resolved_question、clarify、兼容性。

context 只含结构化槽位（上一轮 ``resolved_slots``），项目不保存会话；
全部离线，不连网络。
"""

from __future__ import annotations

import pytest

from finkb.qa import pipeline, understanding

pytestmark = pytest.mark.skipif(
    not pipeline.FACT_DB.exists(), reason="需要开发语料 data/facts.duckdb（仓库不含数据）"
)

Q1 = "比亚迪 2025 年营业收入是多少？"


@pytest.fixture(scope="module")
def first() -> dict:
    return pipeline.ask(Q1, synthesize=False)


def _ctx(payload: dict) -> dict:
    return {"previous_slots": payload["resolved_slots"]}


def test_envelope_fields_present(first):
    for key in ("resolved_question", "resolved_slots", "inherited", "overridden", "missing", "clarify", "context_used"):
        assert key in first
    assert first["resolved_question"] == Q1
    assert first["inherited"] == []
    assert first["context_used"] is False
    assert first["resolved_slots"]["companies"] == ["002594"]
    assert first["resolved_slots"]["metric"] == "revenue"


def test_inherit_company_and_metric_new_period(first):
    payload = pipeline.ask("那 2024 年呢？", synthesize=False, context=_ctx(first))
    assert payload["tier"] == "metric"
    assert payload["resolved_question"].startswith("比亚迪 2024 年")
    assert "营业总收入" in payload["resolved_question"]
    assert "company" in payload["inherited"] and "metric" in payload["inherited"]
    assert "period" in payload["overridden"]
    assert payload["context_used"] is True
    assert "2024" in payload["text"]


def test_inherit_period_and_metric_new_company(first):
    payload = pipeline.ask("美的集团呢？", synthesize=False, context=_ctx(first))
    assert payload["resolved_question"].startswith("美的集团 2025 年")
    assert "period" in payload["inherited"] and "metric" in payload["inherited"]
    assert "company" in payload["overridden"]


def test_explicit_metric_overrides_inherited(first):
    payload = pipeline.ask("美的集团 2025 年存货是多少？", synthesize=False, context=_ctx(first))
    assert payload["resolved_slots"]["metric"] == "inventory"
    assert "metric" in payload["overridden"]


def test_previous_question_fallback(first):
    payload = pipeline.ask("那 2024 年呢？", synthesize=False, context={"previous_question": Q1})
    assert "company" in payload["inherited"] and "metric" in payload["inherited"]


def test_no_context_unchanged():
    left = pipeline.ask(Q1, synthesize=False)
    right = pipeline.ask(Q1, synthesize=False)
    assert left["text"] == right["text"]
    assert left["citations"] == right["citations"]
    assert left["resolved_question"] == Q1
    assert left["context_used"] is False


def test_clarify_when_slots_missing():
    payload = pipeline.ask("营业收入是多少？", synthesize=False)
    assert payload["refused"] is True
    assert payload["clarify"]["missing"] == ["company", "period"]
    assert payload["clarify"]["candidates"]["company"]


def test_context_resolves_clarify(first):
    payload = pipeline.ask("营业收入是多少？", synthesize=False, context=_ctx(first))
    assert payload["clarify"] is None
    assert payload["tier"] == "metric" and not payload["refused"]


def test_aggregate_question_has_no_clarify():
    payload = pipeline.ask("哪些公司 2025 年的资产负债率超过 70%？", synthesize=False)
    assert payload["clarify"] is None


def test_source_doc_not_inherited(first):
    slots = {**first["resolved_slots"], "source_doc": "002594_2025_年报"}
    payload = pipeline.ask("比亚迪 2024 年营业收入是多少？", synthesize=False, context={"previous_slots": slots})
    assert payload["resolved_slots"]["source_doc"] is None


def test_understanding_apply_context_unit():
    context = {
        "previous_slots": {
            "companies": ["002594"],
            "company_names": ["比亚迪"],
            "periods": ["2025"],
            "metric": "revenue",
            "qualifiers": ["海外"],
        }
    }
    u = understanding.understand("那 2024 年呢？", context=context)
    assert u.companies == ["002594"]
    assert u.periods == ["2024"]
    assert u.metric is not None and u.metric.metric_id == "revenue"
    assert u.qualifiers == ["海外"]
    assert set(u.inherited) == {"company", "metric", "qualifier"}


def test_metric_override_recorded(first):
    payload = pipeline.ask("那归母净利润呢？", synthesize=False, context=_ctx(first))
    assert payload["resolved_slots"]["metric"] == "net_profit"
    assert "metric" in payload["overridden"]
    assert "company" in payload["inherited"] and "period" in payload["inherited"]


def test_query_metric_not_silently_inherited(first):
    """毛利率是 SQL 计算指标，不在事实字典里：追问时不能悄悄退回上一轮指标。"""
    payload = pipeline.ask("那毛利率呢？", synthesize=False, context=_ctx(first))
    assert payload["resolved_slots"]["metric"] is None
    assert "metric" not in payload["inherited"]
    assert "毛利率" in payload["resolved_question"]


def test_bare_followup_merges_previous_intent():
    question = "哪些公司 2025 年资产负债率超过 70%？"
    first = pipeline.ask(question, synthesize=False)
    context = {"previous_question": question, "previous_slots": first["resolved_slots"]}
    payload = pipeline.ask("那 2024 年呢？", synthesize=False, context=context)
    assert payload["tier"] == "ontology"
    assert payload["resolved_question"] == "哪些公司 2024 年资产负债率超过 70%？"


def test_note_followup_keeps_intent(first):
    first = pipeline.ask("比亚迪 2025 年存货是多少？", synthesize=False)
    payload = pipeline.ask("附注呢？", synthesize=False, context=_ctx(first))
    assert payload["tier"] == "ontology"
    assert "附注页" in payload["resolved_question"]
    assert "附注页" in payload["text"]
