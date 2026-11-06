"""问句理解与事实通道的回归测试（对应 QA 问题记录里的 5 类问题）。"""

from __future__ import annotations

import json

import pytest

from finkb.qa import fact_lane, synthesizer, understanding
from finkb.qa.fact_lane import FACT_DB
from finkb.search import hybrid
from finkb.search.embed import FakeEmbedder


@pytest.mark.parametrize(
    ("question", "period"),
    [
        ("比亚迪 2025 年第一季度营业收入", "2025Q1"),
        ("比亚迪 2025 年第1季度营业收入", "2025Q1"),
        ("比亚迪 2025 年1季度营业收入", "2025Q1"),
        ("比亚迪 2025Q1 营业收入", "2025Q1"),
        ("比亚迪 2025 年上半年营业收入", "2025H1"),
        ("比亚迪 2025 年前三季度营业收入", "2025Q1-Q3"),
        ("比亚迪 2025 年三季报营业收入", "2025Q1-Q3"),
        ("比亚迪 2025 年全年营业收入", "2025"),
    ],
)
def test_period_variants(question, period):
    slot = understanding.understand(question)
    assert slot.periods == [period]
    assert slot.unresolved == []


def test_bare_period_inherits_context_year():
    slot = understanding.understand("比亚迪 2025 年一季报和半年报的营业总收入分别是多少亿元？")
    assert slot.periods == ["2025Q1", "2025H1"]
    assert slot.unresolved == []
    slot = understanding.understand("赛力斯 2025 年一季报和三季报的净利润分别是多少？")
    assert slot.periods == ["2025Q1", "2025Q1-Q3"]


def test_periods_keep_question_order():
    slot = understanding.understand("比亚迪 2026 年上半年和 2025 年一季度的营业收入")
    assert slot.periods[:2] == ["2026H1", "2025Q1"]


def test_fuzzy_company_match():
    slot = understanding.understand("塞力斯2025年第一季度营收是多少")
    assert slot.companies == ["601127"]
    assert any("近似匹配" in note for note in slot.notes)


def test_unparsed_time_precision_is_flagged():
    slot = understanding.understand("赛力斯 2025 年 3 月的营业收入是多少")
    assert slot.unresolved  # 月度不支持，必须显式暴露，不能默认年报


def test_residual_qualifiers():
    assert "海外" in understanding.residual_qualifiers("比亚迪 2025 年海外收入是多少亿元？")
    assert "分产品" in understanding.residual_qualifiers("美的集团 2025 年分产品的营业收入是多少？")


_requires_facts = pytest.mark.skipif(
    not FACT_DB.exists(), reason="需要开发语料 data/facts.duckdb（仓库不含数据）"
)


@_requires_facts
def test_fact_answer_keeps_raw_value_and_period_label():
    payload = fact_lane.answer("比亚迪 2025 年营业收入是多少亿元？")
    assert payload is not None and not payload["refused"]
    text, _ = synthesizer.render(payload["text"], payload["ledger"])
    assert "8039.65" in text
    assert "803,964,958,000.00 元" in text
    assert "2025 年" in text


def test_fact_answer_refuses_unparsed_period():
    payload = fact_lane.answer("赛力斯 2025 年 3 月的营业收入是多少")
    assert payload is not None and payload["refused"] is True
    assert "时间限定" in payload["text"]


@_requires_facts
def test_fact_answer_period_is_human_readable():
    payload = fact_lane.answer("赛力斯 2025 年第一季度营业收入是多少")
    assert payload is not None and not payload["refused"]
    text, _ = synthesizer.render(payload["text"], payload["ledger"])
    assert "2025 年第一季度" in text
    assert "191.47" in text


def _tiny_index(tmp_path):
    rows = [
        {"chunk_id": "a:p1:0", "doc_id": "a", "company": "000001", "period": "2025",
         "page_num": 1, "section": "管理层讨论", "block_type": "text",
         "text": "本集团的投资银行业务包括股权融资、债务融资和财务顾问业务。", "table_header": None},
        {"chunk_id": "a:p2:0", "doc_id": "a", "company": "000001", "period": "2025",
         "page_num": 2, "section": "其他", "block_type": "text",
         "text": "如何如何如何如何如何如何如何如何如何如何如何如何如何如何如何如何如何如何如何如何", "table_header": None},
    ]
    path = tmp_path / "chunks.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return path


def test_phrase_match_beats_function_words(tmp_path):
    chunks = hybrid.load_chunks(_tiny_index(tmp_path))
    results = hybrid.search("投资银行业务包括哪些内容", chunks=chunks, embedder=None)
    assert results and results[0]["chunk_id"] == "a:p1:0"


def test_section_match_gets_boost(tmp_path):
    chunks = [
        {"chunk_id": "b:p1:0", "doc_id": "b", "company": "000001", "period": "2025", "page_num": 1,
         "section": "(一) 环境风险", "block_type": "text", "text": "风险相关说明。", "table_header": None},
        {"chunk_id": "b:p2:0", "doc_id": "b", "company": "000001", "period": "2025", "page_num": 2,
         "section": "其他", "block_type": "text", "text": "风险风险风险风险风险风险风险。", "table_header": None},
    ]
    results = hybrid.search("环境风险", chunks=chunks, embedder=None)
    assert results and results[0]["chunk_id"] == "b:p1:0"


def test_document_qualifier_extracts_source_doc():
    from finkb.qa.understanding import understand

    u = understand("比亚迪2026年一季报里2025年第一季度的基本每股收益是多少")
    assert u.source_doc == "002594_2026_一季报"
    assert u.document_period == "2026Q1"
    assert u.periods == ["2025Q1"]

    u2 = understand("比亚迪2025年一季报披露的基本每股收益")
    assert u2.source_doc == "002594_2025_一季报"
    assert u2.periods == ["2025Q1"]
