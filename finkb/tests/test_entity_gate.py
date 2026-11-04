"""实体名门禁（entity_gate）单测。"""

from __future__ import annotations

from finkb.fact_engine import entity_gate as gate


def test_normalize_strips_arabic_numbering():
    assert gate.normalize("（1）甲公司") == "甲公司"
    assert gate.normalize("1、甲公司") == "甲公司"
    assert gate.normalize("① 甲公司") == "甲公司"


def test_normalize_keeps_chinese_numeral_prefix():
    # 回归：曾经把「三峡」当成序号「三」削成「峡」。
    assert gate.normalize("三峡财务有限责任公司") == "三峡财务有限责任公司"
    assert gate.normalize("五家渠市某某公司") == "五家渠市某某公司"


def test_gate_rejects_accounting_labels():
    for name in ["合计", "其中：营业收入", "占净资产比例", "期末余额", "姓名", "1,234.00"]:
        assert gate.gate_reason(name) is not None, name


def test_gate_accepts_company_and_person():
    assert gate.gate_reason("宁德时代新能源科技股份有限公司") is None
    assert gate.gate_reason("张三") is None


def test_gate_rejects_short_and_long():
    assert gate.gate_reason("甲") == "too_short"
    assert gate.gate_reason("甲" * 80) == "too_long"


def test_match_keys_merges_suffix_variants():
    keys = gate.match_keys("某某有限公司")
    assert "某某有限公司" in keys and "某某" in keys
    assert gate.match_keys("某某")[0] == "某某"


def test_match_keys_removes_parenthetical():
    keys = gate.match_keys("某某（中国）有限公司")
    assert "某某" in keys


def test_gate_config_override(tmp_path):
    cfg = {"skip_exact": ["测试公司"], "skip_tokens": [], "min_length": 2, "max_length": 60}
    assert gate.gate_reason("测试公司", cfg) == "generic"
    assert gate.gate_reason("测试集团", cfg) is None
