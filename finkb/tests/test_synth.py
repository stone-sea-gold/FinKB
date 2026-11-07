"""合成器（P1 契约）单测：提示词、FakeBackend、结果规范化、失败回退。

全部离线：不联网、不调真实 LLM。
"""

from __future__ import annotations

from finkb.qa.evidence import build_pack
from finkb.qa.synth import FakeBackend, build_prompt, compose, render_evidence


def _pack():
    envelope = {
        "tier": "metric",
        "route": "fact",
        "ledger": [
            {
                "id": "c1",
                "value": 2334.33,
                "unit": "亿元",
                "fact_id": 1,
                "doc": "000002_2025_年报",
                "page": 11,
                "metric_id": "revenue",
                "company": "000002",
                "period": "2025",
                "role": "原报告",
            }
        ],
        "data": {"notes": []},
    }
    return build_pack("万科A 2025 年营业收入是多少？", envelope)


def test_prompt_contains_rules_and_evidence():
    pack = _pack()
    system, user = build_prompt("万科A 2025 年营业收入是多少？", pack)
    assert "{{cN}}" in system
    assert "refused" in system
    assert "万科A 2025 年营业收入是多少？" in user
    assert "[c1]" in user
    assert "000002_2025_年报#P11" in user


def test_render_evidence_sections():
    pack = _pack()
    text = render_evidence(pack)
    assert "【结构化事实】" in text
    assert "2334.33" in text


def test_fake_backend_returns_result():
    pack = _pack()
    backend = FakeBackend(
        {"answer": "营业收入为 {{c1}} 亿元。[Ref: 000002_2025_年报#P11]", "used_ids": ["c1"]}
    )
    result = compose("万科A 2025 年营业收入是多少？", pack, backend)
    assert result is not None
    assert result.used_ids == ["c1"]
    assert result.backend == "fake"
    assert not result.refused
    assert len(backend.calls) == 1


def test_compose_without_backend_returns_none():
    assert compose("问题", _pack(), None) is None


def test_unknown_ids_are_filtered_and_noted():
    pack = _pack()
    backend = FakeBackend({"answer": "答 {{c9}}", "used_ids": ["c1", "c9"]})
    result = compose("问题", pack, backend)
    assert result is not None
    assert result.used_ids == ["c1"]
    assert any("未知证据 id" in note for note in result.notes)


def test_retry_then_success():
    pack = _pack()
    backend = FakeBackend(
        [
            {"bad": True},
            {"answer": "第二次成功", "used_ids": []},
        ]
    )
    result = compose("问题", pack, backend, max_retries=1)
    assert result is not None
    assert result.answer == "第二次成功"
    assert len(backend.calls) == 2


def test_refused_without_answer_is_valid():
    pack = _pack()
    backend = FakeBackend({"answer": "", "refused": True, "missing": ["2025Q1 数据"]})
    result = compose("问题", pack, backend)
    assert result is not None
    assert result.refused is True
    assert result.missing == ["2025Q1 数据"]


def test_result_to_dict():
    result = compose("问题", _pack(), FakeBackend({"answer": "答", "used_ids": []}))
    data = result.to_dict()
    assert data["backend"] == "fake"
    assert data["answer"] == "答"
    assert isinstance(data["used_ids"], list)
