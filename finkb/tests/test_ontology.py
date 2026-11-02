"""本体层（四层概念栈 / 约束校验 / 六跳链条）的离线测试。

这组测试守的是「可追溯」这条底线：每个 L2 科目必须能指回国标里真实存在的
元素，每个数字必须能走回页码与原文。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from finkb.fact_engine.metric_dict import Metric
from finkb.fact_engine.store import FactStore
from finkb.ontology import audit, chain, check, expr, loader, model

# 仓库根的 .scratch/cas/（由 tools/build_ontology.py 抽取生成，非仓库资产）
CAS_LABELS = (
    __import__("pathlib").Path(__file__).resolve().parents[2]
    / ".scratch" / "cas" / "labels_cas2015.json"
)


@pytest.fixture()
def store() -> FactStore:
    s = FactStore(":memory:")
    s.load_metric_dict([
        Metric("revenue", "营业总收入", "元", "income", ()),
        Metric("operating_cost", "营业成本", "元", "income", ()),
        Metric("total_assets", "资产总计", "元", "balance", ()),
        Metric("total_liabilities", "负债合计", "元", "balance", ()),
        Metric("total_equity", "所有者权益合计", "元", "balance", ()),
        Metric("net_assets", "归母净资产", "元", "balance", ()),
        Metric("minority_interest", "少数股东权益", "元", "balance", ()),
        Metric("eps_basic", "基本每股收益", "元/股", "per_share", ()),
        Metric("roe_weighted", "加权平均净资产收益率", "%", "per_share", ()),
        Metric("ocf", "经营活动现金流量净额", "元", "cashflow", ()),
        Metric("icf", "投资活动现金流量净额", "元", "cashflow", ()),
        Metric("fcf", "筹资活动现金流量净额", "元", "cashflow", ()),
        Metric("net_cash_increase", "现金净增加额", "元", "cashflow", ()),
        Metric("fx_effect", "汇率变动对现金的影响", "元", "cashflow", ()),
    ])
    doc = model.load()
    loader.ensure_schema(s.conn)
    loader.load(s.conn, doc)
    yield s
    s.close()


def _insert(store: FactStore, metric_id: str, value: float, **kw) -> None:
    defaults = dict(
        company="002594", period="2025", metric_id=metric_id, unit_family="money",
        unit_of_account="元", norm_unit="亿元", value_raw=value * 100_000_000,
        raw_text_matched=f"{value * 100_000_000:,.2f}", value_norm=value,
        caliber="合并报表", reporting_party="比亚迪股份有限公司",
        report_date=date(2026, 3, 27),
        source_doc="002594_2025_年报", page_num=11, table_id="002594_2025_年报#P11_T07",
        match_level="exact",
    )
    defaults.update(kw)
    store.insert_fact(**defaults)


# ---------------------------------------------------------------- YAML


def test_yaml_loads_without_warnings() -> None:
    doc = model.load()
    assert doc.warnings == []
    assert doc.version >= 1
    assert set(doc.layers) == {"L0", "L1", "L2", "L3"}


def test_every_concept_parent_exists_and_layer_is_legal() -> None:
    doc = model.load()
    ids = {c.concept_id for c in doc.concepts}
    assert len(ids) == len(doc.concepts), "concept_id 有重复"
    for c in doc.concepts:
        assert c.layer in model.LAYERS
        if c.parent_id:
            assert c.parent_id in ids, f"{c.concept_id} 的 parent 不存在"


def test_l2_basis_element_really_exists_in_cas() -> None:
    """防止凭印象填国标元素名——这是整个「可追溯」的地基。

    ``labels_cas2015.json`` 是从财政部 CAS 分类标准 label linkbase 抽出来的
    4317 个元素。若它不在仓库里（CI 没跑抽取），跳过而不是失败。
    """
    if not CAS_LABELS.exists():
        pytest.skip("CAS 标签表未生成（先跑 tools/build_ontology.py 的抽取步骤）")
    labels = json.loads(CAS_LABELS.read_text(encoding="utf-8"))
    doc = model.load()
    missing = []
    for c in doc.concepts:
        if c.layer != "L2" or not c.basis_element:
            continue
        if lst := labels.get(c.basis_element):
            assert isinstance(lst, dict) and lst, c.basis_element
        else:
            missing.append(f"{c.concept_id} -> {c.basis_element}")
    assert not missing, "以下 L2 概念指向了 CAS 里不存在的元素：" + "; ".join(missing)


def test_l3_formulas_only_use_known_names() -> None:
    doc = model.load()
    known = {c.concept_id for c in doc.concepts} | {c.metric_id for c in doc.concepts
                                                    if c.metric_id}
    known |= {a for c in doc.concepts for a in c.expression_args}
    for c in doc.concepts:
        if not c.formula:
            continue
        for v in expr.deps(expr.parse(c.formula)):
            assert v in known, f"{c.concept_id} 的公式引用了未知名字 {v}"


# -------------------------------------------------------------- 载入库


def test_loader_is_idempotent(store: FactStore) -> None:
    doc = model.load()
    first = loader.load(store.conn, doc)
    second = loader.load(store.conn, doc)
    assert first == second
    assert first["concepts"] == len(doc.concepts)
    assert first["relations"] == len(doc.relations)
    assert first["constraints"] == len(doc.constraints)


# ---------------------------------------------------------------- 约束


def test_valid_identity_produces_no_errors(store: FactStore) -> None:
    _insert(store, "total_assets", 1000.0)
    _insert(store, "total_liabilities", 700.0)
    _insert(store, "total_equity", 300.0)
    report = check.run(store.conn)
    assert report["errors"] == 0


def test_broken_identity_is_reported_as_error(store: FactStore) -> None:
    _insert(store, "total_assets", 1000.0)
    _insert(store, "total_liabilities", 700.0)
    _insert(store, "total_equity", 200.0)  # 差 100 亿，远超容差
    report = check.run(store.conn)
    assert report["errors"] >= 1
    hit = [v for v in report["violations"]
           if v["constraint_id"] == "acct.assets_eq_liabilities_plus_equity"]
    assert hit and hit[0]["severity"] == "error"


def test_warn_severity_does_not_count_as_error(store: FactStore) -> None:
    """ROE 出界是业务异动（超大亏损或净资产极薄），只能报警告、不能判错。"""
    _insert(store, "roe_weighted", 300.0)  # 超出 range.roe 的 [-100, 200]
    report = check.run(store.conn)
    assert report["errors"] == 0
    assert any(v["constraint_id"] == "range.roe" for v in report["violations"])


def test_cashflow_identity_needs_fx_effect(store: FactStore) -> None:
    """现金流恒等式是四项：缺了汇率变动项就永远对不上（v3 修正）。"""
    for mid, val in (("ocf", 38.0), ("icf", 23.8), ("fcf", -75.0),
                     ("fx_effect", -166.4), ("net_cash_increase", -179.6)):
        _insert(store, mid, val)
    report = check.run(store.conn)
    assert not [v for v in report["violations"]
                if v["constraint_id"] == "acct.cashflow_sum"]


def test_equity_split_allows_negative_minority_interest(store: FactStore) -> None:
    """少数股东权益为负时归母净资产可以大于权益合计——恒等式仍然成立。"""
    _insert(store, "net_assets", 122.64)
    _insert(store, "minority_interest", -3.59)
    _insert(store, "total_equity", 119.05)  # 122.64 - 3.59
    report = check.run(store.conn)
    assert not [v for v in report["violations"]
                if v["constraint_id"] == "acct.equity_split"]
    # 金额对不上时必须报错
    _insert(store, "net_assets", 122.64, period="2024")
    _insert(store, "minority_interest", -3.59, period="2024")
    _insert(store, "total_equity", 200.0, period="2024")
    report = check.run(store.conn)
    hit = [v for v in report["violations"] if v["constraint_id"] == "acct.equity_split"]
    assert hit and hit[0]["severity"] == "error"


def test_expense_positive_catches_sign_bug(store: FactStore) -> None:
    """费用类入库统一取正数，负数必是实现 bug。"""
    _insert(store, "operating_cost", 850.0)
    assert not [v for v in check.run(store.conn)["violations"]
                if v["constraint_id"] == "acct.expense_positive"]
    _insert(store, "operating_cost", -850.0, period="2024")
    assert [v for v in check.run(store.conn)["violations"]
            if v["constraint_id"] == "acct.expense_positive"]


def test_unit_check_reads_norm_unit(store: FactStore) -> None:
    """每股指标的原始表头单位常写成「元」，验的必须是 norm_unit。"""
    _insert(store, "eps_basic", 3.57, unit_family="pershare",
            unit_of_account="元", norm_unit="元/股")
    report = check.run(store.conn)
    assert not [v for v in report["violations"]
                if v["constraint_id"] == "unit.eps_is_per_share"]
    _insert(store, "eps_basic", 3.58, unit_family="pershare",
            unit_of_account="元", norm_unit="元", period="2024")
    report = check.run(store.conn)
    assert [v for v in report["violations"]
            if v["constraint_id"] == "unit.eps_is_per_share"]


# ---------------------------------------------------------------- 体检


def test_audit_clean_chain_has_no_structural_break(store: FactStore) -> None:
    _insert(store, "revenue", 8039.65)
    report = audit.audit(store.conn)
    assert report["total_facts"] == 1
    for hop in ("concept", "ancestry", "constraints", "evidence"):
        assert report["hops"][hop] == 0, hop


def test_audit_flags_metric_without_concept(store: FactStore) -> None:
    _insert(store, "not_in_dict", 1.0)
    report = audit.audit(store.conn)
    assert report["hops"]["concept"] == 1
    assert report["samples"]["concept"][0]["metric_id"] == "not_in_dict"


def test_audit_flags_incomplete_evidence(store: FactStore) -> None:
    _insert(store, "revenue", 8039.65, raw_text_matched="")
    report = audit.audit(store.conn)
    assert report["hops"]["evidence"] == 1


# ---------------------------------------------------------------- 链条


def test_chain_walks_concept_to_evidence(store: FactStore) -> None:
    _insert(store, "revenue", 8039.65)
    out = chain.resolve(store.conn, "revenue", company="002594", period="2025")
    assert out["concept"]["concept_id"] == "metric:revenue"
    assert out["ancestry"], "祖先链不能为空"
    assert out["facts"] and out["facts"][0]["evidence"]["complete"] is True
    # 测试库里没有 claims 账本（它属于 wiki.duckdb），第⑥跳为空是预期，不算断链。
    assert all(b["hop"] == "pages" for b in out["breaks"])


def test_chain_resolves_l3_derived_value(store: FactStore) -> None:
    """L3 的第五跳不是一条事实，而是按公式的确定性计算——这条链也要能走。"""
    _insert(store, "revenue", 1000.0)
    _insert(store, "operating_cost", 850.0)
    out = chain.resolve(store.conn, "self:gross_margin", company="002594",
                        period="2025", with_pages=False)
    assert out["concept"]["layer"] == "L3"
    assert out["derived"]["value"] == pytest.approx(15.0)  # 百分数口径
    assert {a["metric_id"] for a in out["derived"]["args"]} == {"revenue", "operating_cost"}
    assert out["derived"]["args"][0]["facts"] == 1


def test_chain_reports_missing_fact(store: FactStore) -> None:
    out = chain.resolve(store.conn, "revenue", company="000001", period="2025")
    assert out["facts"] == []
    assert any(b["hop"] == "facts" for b in out["breaks"])


def test_derived_metric_computed_from_facts(store: FactStore) -> None:
    _insert(store, "revenue", 1000.0)
    _insert(store, "operating_cost", 850.0)
    from finkb.ontology import derive
    values, errors = derive.compute(store.conn, "002594", "2025")
    # 只插了两条事实，其余 L3 报「缺参数」是预期行为；无参数据不可算就不该硬算。
    assert all("缺参数" in e for e in errors)
    assert values["self:gross_margin"] == pytest.approx(15.0)  # 百分数口径
