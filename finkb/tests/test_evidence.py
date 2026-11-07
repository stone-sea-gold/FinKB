"""证据包（P1 契约）单测：构造、冲突识别、预算裁剪、序列化。

全部离线：不读 DuckDB、不联网、不调 LLM。
"""

from __future__ import annotations

from finkb.qa.evidence import build_pack, from_envelope


def _metric_envelope() -> dict:
    return {
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
                "raw_value": 233433000000.0,
                "raw_unit": "元",
                "role": "原报告",
                "report_date": "2026-03-31",
            },
            {
                "id": "c2",
                "value": 2300.0,
                "unit": "亿元",
                "fact_id": 2,
                "doc": "000002_2026_一季报",
                "page": 1,
                "metric_id": "revenue",
                "company": "000002",
                "period": "2025",
                "role": "对比列",
                "report_date": "2026-04-30",
            },
            {"id": "c3", "value": 12.3, "unit": "%", "kind": "derived", "formula": "yoy"},
            {
                "id": "c4",
                "value": 2334.32969,
                "unit": "亿元",
                "kind": "alternative",
                "doc": "000002_2025_年报",
                "page": 11,
                "role": "原报告",
            },
        ],
        "data": {"notes": ["同一指标存在多个口径。"]},
    }


def test_metric_pack_shape_and_metric_name():
    pack = build_pack("万科A 2025 年营业收入是多少？", _metric_envelope())
    assert pack.tier == "metric" and pack.route == "fact"
    assert pack.counts() == {"facts": 4, "inferences": 0, "passages": 0, "conflicts": 1}
    assert pack.ids() == {"c1", "c2", "c3", "c4", "x1"}
    assert pack.facts[0].metric_name  # 指标字典可读时应有中文名
    assert pack.facts[2].kind == "derived" and pack.facts[2].formula == "yoy"
    assert "同一指标存在多个口径。" in pack.notes


def test_conflict_detects_different_values():
    pack = build_pack("万科A 2025 年营业收入是多少？", _metric_envelope())
    conflict = pack.conflicts[0]
    assert conflict.metric_id == "revenue"
    assert set(conflict.fact_ids) == {"c1", "c2"}
    assert len(conflict.values) == 2


def test_budget_trims_and_notes():
    pack = build_pack(
        "万科A 2025 年营业收入是多少？",
        _metric_envelope(),
        budget={"max_facts": 1, "max_chars": 1000},
    )
    assert len(pack.facts) == 1
    assert pack.budget["max_facts"] == 1
    assert any("裁剪" in note for note in pack.notes)


def test_char_budget_can_empty_pack():
    pack = build_pack(
        "万科A 2025 年营业收入是多少？",
        _metric_envelope(),
        budget={"max_facts": 8, "max_chars": 5},
    )
    assert pack.facts == []
    assert any("裁剪" in note for note in pack.notes)


def test_text_pack_passages_and_table_detection():
    envelope = {
        "tier": "document",
        "route": "text",
        "ledger": [],
        "data": {
            "evidence": [
                {
                    "chunk_id": "002594_2025_年报:p220:4",
                    "doc_id": "002594_2025_年报",
                    "page_num": 220,
                    "section": "现金及现金等价物",
                    "snippet": "| 项目 | 2025年 |\n| 现金 | 100 |",
                },
                {
                    "chunk_id": "002594_2025_年报:p18:2",
                    "doc_id": "002594_2025_年报",
                    "page_num": 18,
                    "section": "海外布局",
                    "snippet": "公司持续推进海外市场布局。",
                },
            ]
        },
    }
    pack = build_pack("比亚迪 2025 年报里海外布局如何？", envelope)
    assert pack.counts()["passages"] == 2
    assert pack.passages[0].block_type == "table"
    assert pack.passages[1].block_type == "text"
    assert not pack.is_empty()


def test_ontology_pack_inference():
    envelope = {
        "tier": "ontology",
        "route": "ontology",
        "data": {"class_id": "finkb:HighLeverage", "members": [{"company": "000002"}]},
    }
    pack = build_pack("哪些公司资产负债率超过 70%？", envelope)
    assert pack.counts()["inferences"] == 1
    assert pack.inferences[0].kind == "class"
    assert pack.inferences[0].rule_id == "finkb:HighLeverage"


def test_refused_envelope_keeps_note():
    envelope = {"tier": "metric", "route": "fact", "refused": True, "text": "没有找到对应的事实数据。"}
    pack = build_pack("2027 年比亚迪营收？", envelope)
    assert pack.is_empty()
    assert any("没有找到" in note for note in pack.notes)


def test_pack_to_dict_serializable():
    pack = build_pack("万科A 2025 年营业收入是多少？", _metric_envelope())
    data = pack.to_dict()
    assert data["tier"] == "metric"
    assert isinstance(data["facts"], list) and data["facts"][0]["id"] == "c1"
    assert data["budget"]["max_facts"] == 8
