"""三路问答（qa/）单测：槽位、路由、渲染、校验；全部离线。"""

from __future__ import annotations

import json

import pytest

from finkb.qa import fact_lane, router, synthesizer, verifier


def test_slots_company_period_metric():
    slot = fact_lane.extract("万科A（000002）2025 年的营业收入是多少亿元？")
    assert slot.companies == ["000002"]
    assert slot.periods == ["2025"]
    assert slot.metric is not None and slot.metric.metric_id == "revenue"


def test_company_short_alias():
    slot = fact_lane.extract("万科 2025 年营业收入是多少？")
    assert slot.companies == ["000002"]


def test_period_normalization():
    slot = fact_lane.extract("比亚迪 2026 年上半年和 2025 年一季度的营业收入")
    assert slot.periods[:2] == ["2026H1", "2025Q1"]


def test_operations():
    assert fact_lane.extract("比亚迪 2025 年营业收入同比增速是多少？").operation == "yoy"
    assert fact_lane.extract("比亚迪 2025 年营业收入比 2024 年增加多少？").operation == "diff_add"
    assert fact_lane.extract("三家公司 2025 年营业收入合计是多少？").operation == "sum"
    assert fact_lane.extract("五家公司 2025 年营业收入的最高值比最低值高多少？").operation == "range"


def test_router_three_lanes():
    assert router.route("万科A 2025 年的营业收入是多少亿元？").name == "fact"
    assert router.route("哪些公司 2025 年的资产负债率超过 70%？").name == "ontology"
    assert router.route("比亚迪 2025 年报里，公司如何描述海外市场的区域布局？").name == "text"
    assert router.route("比亚迪 2025 年存货的附注页是哪一页？").name == "ontology"


def test_synthesizer_render():
    ledger = [
        {"id": "c1", "value": 2334.32769, "unit": "亿元"},
        {"id": "c2", "value": 4, "unit": "家", "format": "int"},
    ]
    text, used = synthesizer.render("收入 {{c1}} 亿元，共 {{c2}} 家。", ledger)
    assert text == "收入 2334.33 亿元，共 4 家。"
    assert used == ["c1", "c2"]


def test_verifier_ok_and_bad(tmp_path, monkeypatch, request):
    parsed = tmp_path / "parsed"
    doc = parsed / "000001_2025_年报"
    doc.mkdir(parents=True)
    (doc / "report.json").write_text(json.dumps({"pages": 2}), encoding="utf-8")
    (doc / "page_anchored.md").write_text(
        '<!-- META: {"doc": "000001_2025_年报", "page": 1} -->\n营业收入 100 亿元\n', encoding="utf-8"
    )
    monkeypatch.setattr(verifier, "PARSED_DIR", parsed)
    verifier._page_texts.cache_clear()
    verifier._report_pages.cache_clear()
    request.addfinalizer(verifier._page_texts.cache_clear)
    request.addfinalizer(verifier._report_pages.cache_clear)
    payload = {
        "route": "fact",
        "text": "营业收入 {{c1}} 亿元。",
        "ledger": [{"id": "c1", "value": 100.0}],
        "citations": ["[Ref: 000001_2025_年报#P1]"],
    }
    payload["text"] = synthesizer.render(payload["text"], payload["ledger"])[0]
    assert verifier.verify(payload)["ok"] is True
    payload["text"] = "营业收入 999 亿元。"
    assert verifier.verify(payload)["ok"] is False


def test_negative_out_of_scope_refuses():
    from finkb.qa.answer import answer

    assert answer("比亚迪 2025 年的港股股价是多少？")["refused"] is True
    assert answer("比亚迪 2025 年年报披露的在职员工总人数是多少？")["refused"] is True


def test_fact_lane_matches_gold_if_db_exists():
    from finkb.qa.answer import answer
    from finkb.qa.fact_lane import FACT_DB

    if not FACT_DB.exists():
        pytest.skip("facts.duckdb 不存在")
    payload = answer("万科A（000002）2025 年的营业收入是多少亿元？")
    assert payload["refused"] is False
    assert abs(payload["value"] - 2334.32769) < 1e-6


def test_router_text_intent_beats_fact():
    # 有指标词但问的是「规模/人员」→ 走 text，不走 fact
    assert router.route("美的集团 2025 年的研发投入和研发人员规模如何？").name == "text"
    # 纯数值问法仍然走 fact
    assert router.route("美的集团 2025 年的研发费用是多少亿元？").name == "fact"

def test_table_snippet_keeps_full_table():
    from finkb.qa.answer import _table_snippet

    text = (
        "| 项目 | 2025年 | 2024年 |\n"
        "| --- | --- | --- |\n"
        "| 现金 | 68,395,197 | 102,256,542 |\n"
        "| 年末现金及现金等价物余额 | 68,395,197 | 102,256,542 |"
    )
    out = _table_snippet(text)
    assert "现金" in out
    assert "68,395,197" in out
    assert "年末现金及现金等价物余额" in out
    assert "---" not in out


def test_fact_lane_prefers_requested_report_if_db_exists():
    from finkb.qa.fact_lane import FACT_DB

    if not FACT_DB.exists():
        pytest.skip("facts.duckdb 不存在")
    result = fact_lane.answer("比亚迪2025年一季报披露的基本每股收益")
    assert result and result["refused"] is False
    assert result["ledger"][0]["doc"] == "002594_2025_一季报"
    assert abs(result["ledger"][0]["value"] - 3.12) < 1e-9
    assert result["citations"] == ["[Ref: 002594_2025_一季报#P7]"]


def test_fact_lane_discloses_comparative_column_if_db_exists():
    from finkb.qa.fact_lane import FACT_DB

    if not FACT_DB.exists():
        pytest.skip("facts.duckdb 不存在")
    result = fact_lane.answer("比亚迪2026年一季报里2025年第一季度的基本每股收益")
    assert result and result["refused"] is False
    assert result["ledger"][0]["doc"] == "002594_2026_一季报"
    text, _ = synthesizer.render(result["text"], result["ledger"])
    assert "对比列" in text
    assert "3.12" in text


def test_fact_lane_prefers_same_year_report_if_db_exists():
    from finkb.qa.fact_lane import FACT_DB

    if not FACT_DB.exists():
        pytest.skip("facts.duckdb 不存在")
    result = fact_lane.answer("赛力斯2025年第一季度营业收入是多少")
    assert result and result["refused"] is False
    assert result["ledger"][0]["doc"] == "601127_2025_一季报"


def test_content_query_drops_company_period_report_type() -> None:
    """检索前剔除公司/期间/报告类型，避免这些词在目标文档内淹没内容词。"""
    from finkb.qa.answer import _content_query
    from finkb.qa.understanding import understand

    question = "中国神华 2025 年报里，公司如何描述煤炭产销情况？"
    slot = understand(question)
    cleaned = _content_query(question, slot)
    assert "中国神华" not in cleaned
    assert "2025" not in cleaned
    assert "年报" not in cleaned
    assert "煤炭" in cleaned and "产销" in cleaned
