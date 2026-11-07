"""响应契约（P6）：版本号、免责声明、多轮字段与模式 B 责任声明。

全部离线；需要开发语料（仓库不含数据）时跳过。
"""

from __future__ import annotations

import json

import pytest

from finkb.qa import mcp_server
from finkb.qa import pipeline
from finkb.qa.mcp_server import CHUNKS_PATH, FACT_DB
from finkb.qa.synth import FakeBackend

pytestmark = pytest.mark.skipif(
    not (FACT_DB.exists() and CHUNKS_PATH.exists()),
    reason="需要开发语料 facts.duckdb / chunks.jsonl（仓库不含数据）",
)

ENVELOPE_KEYS = {
    "question", "tier", "route", "refused", "confidence",
    "data", "ledger", "citations", "text", "trace",
    "schema_version", "disclaimer",
}
TEXT_Q = "比亚迪 2025 年报里，公司如何描述海外市场的区域布局？"


def _call(name: str, arguments: dict) -> dict:
    response = mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    result = response["result"]
    assert not result["isError"], result
    return json.loads(result["content"][0]["text"])


def test_metric_envelope_has_version_and_disclaimer():
    payload = pipeline.ask("比亚迪 2025 年营业收入是多少？")
    assert ENVELOPE_KEYS <= set(payload)
    assert payload["schema_version"] == pipeline.SCHEMA_VERSION == "1.0.0"
    assert payload["disclaimer"] == pipeline.DISCLAIMER
    assert payload["tier"] == "metric" and payload["route"] == "fact"
    assert payload["refused"] is False


def test_refusal_envelope_has_version_and_disclaimer():
    payload = pipeline.ask("比亚迪的股价是多少？")
    assert payload["refused"] is True
    assert payload["schema_version"] == pipeline.SCHEMA_VERSION
    assert payload["disclaimer"]
    assert payload["confidence"] == 0.0


def test_context_fields_are_additive():
    first = pipeline.ask("比亚迪 2025 年营业收入是多少？")
    second = pipeline.ask(
        "那 2024 年呢？",
        context={"previous_slots": first["resolved_slots"]},
    )
    for key in ("resolved_question", "resolved_slots", "inherited", "overridden", "missing", "clarify", "context_used"):
        assert key in second
    assert second["context_used"] is True
    assert "company" in second["inherited"] and "metric" in second["inherited"]
    assert "period" in second["overridden"]
    assert second["resolved_question"].startswith("比亚迪 2024 年")


def test_no_context_keeps_resolved_question_identical():
    payload = pipeline.ask("比亚迪 2025 年营业收入是多少？")
    assert payload["context_used"] is False
    assert payload["resolved_question"] == "比亚迪 2025 年营业收入是多少？"
    assert payload["inherited"] == [] and payload["overridden"] == []
    assert payload["clarify"] is None


def test_mode_a_synthesis_marks_mode():
    env = pipeline.ask(TEXT_Q, synthesize=False)
    ref = env["citations"][0]
    answer = f"公司持续推进海外布局，海外业务覆盖多个国家。{ref}"
    backend = FakeBackend([{"answer": answer, "used_ids": [], "refused": False, "missing": [], "notes": []}])
    payload = pipeline.ask(TEXT_Q, synthesize=backend)
    assert payload["synthesis"]["used"] is True
    assert payload["synthesis"]["mode"] == "A"


def test_mode_b_tools_declare_responsibility():
    evidence = _call("evidence", {"question": "比亚迪 2025 年营业收入是多少？", "tier": "metric"})
    assert evidence["mode"] == "B"
    assert evidence["responsibility"]
    assert "evidence" in evidence

    verdict = _call(
        "verify",
        {
            "question": "比亚迪 2025 年营业收入是多少？",
            "tier": "metric",
            "answer": {"answer": "营业收入为 7771.02 亿元。[Ref: 002594_2025_年报#P11]"},
        },
    )
    assert verdict["mode"] == "B"
    assert verdict["responsibility"]
