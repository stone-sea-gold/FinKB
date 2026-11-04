"""本体导出（TBox/ABox）与推理物化（定义类 / 属性链 / OWL 2 RL）的离线测试。

全部用内存或临时 DuckDB + 真实 YAML，不联网。OWL 闭包测试依赖可选包
``rdflib``/``owlrl``；没装时跳过，不阻塞主测试集（AGENTS：测试必须离线可跑）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from finkb.fact_engine.metric_dict import Metric
from finkb.fact_engine.store import FactStore
from finkb.ontology import export, infer, loader, model

METRICS = [
    Metric("revenue", "营业总收入", "亿元", "income", ()),
    Metric("operating_cost", "营业成本", "亿元", "income", ()),
    Metric("net_profit", "净利润", "亿元", "income", ()),
    Metric("total_assets", "资产总计", "亿元", "balance", ()),
    Metric("total_liabilities", "负债合计", "亿元", "balance", ()),
    Metric("total_equity", "所有者权益合计", "亿元", "balance", ()),
    Metric("net_assets", "归母净资产", "亿元", "balance", ()),
    Metric("minority_interest", "少数股东权益", "亿元", "balance", ()),
    Metric("ocf", "经营活动现金流量净额", "亿元", "cashflow", ()),
    Metric("icf", "投资活动现金流量净额", "亿元", "cashflow", ()),
    Metric("fcf", "筹资活动现金流量净额", "亿元", "cashflow", ()),
    Metric("net_cash_increase", "现金净增加额", "亿元", "cashflow", ()),
    Metric("fx_effect", "汇率变动对现金的影响", "亿元", "cashflow", ()),
    Metric("cash", "货币资金", "亿元", "balance", ()),
    Metric("accounts_receivable", "应收账款", "亿元", "balance", ()),
    Metric("inventory", "存货", "亿元", "balance", ()),
    Metric("rd_expense", "研发费用", "亿元", "income", ()),
    Metric("roe_weighted", "加权平均净资产收益率", "%", "per_share", ()),
]


def _add_fact(store: FactStore, metric_id: str, value: float,
              company: str = "000001", period: str = "2025", page: int = 10) -> int:
    return store.insert_fact(
        company=company, period=period, metric_id=metric_id,
        unit_family="money", unit_of_account="元", norm_unit="亿元",
        value_raw=value * 100_000_000, raw_text_matched=f"{value * 100_000_000:,.2f}",
        value_norm=value, caliber="合并报表", reporting_party="测试公司",
        report_date=date(2026, 3, 1), source_doc=f"{company}_{period}_年报",
        page_num=page, table_id=f"{company}#P{page}_T01", match_level="exact",
    )


def _bootstrap(s: FactStore) -> tuple[model.OntologyDoc, model.AxiomDoc]:
    """把字典/本体/公理灌进一个空库；包在事务里，避免每条 INSERT 各自提交。"""
    doc = model.load()
    ax = model.load_axioms(doc=doc)
    s.conn.execute("BEGIN TRANSACTION")
    s.load_metric_dict(METRICS)
    loader.load(s.conn, doc, axioms=ax)
    s.conn.execute("COMMIT")
    return doc, ax


@pytest.fixture()
def mem_store():
    s = FactStore(":memory:")
    doc, ax = _bootstrap(s)
    yield s, doc, ax
    s.close()


@pytest.fixture()
def file_store(tmp_path: Path):
    db = tmp_path / "facts.duckdb"
    s = FactStore(db)
    doc, ax = _bootstrap(s)
    yield s, db, doc, ax
    s.close()


# ---------------------------------------------------------------- 定义类


def test_materialize_defined_classes(mem_store) -> None:
    s, _doc, ax = mem_store
    _add_fact(s, "total_assets", 1000.0)
    _add_fact(s, "total_liabilities", 800.0)     # 杠杆 0.8 → HighLeverage
    _add_fact(s, "net_profit", 100.0)
    _add_fact(s, "ocf", 10.0)                    # ocf/净利 0.1 → CashflowDivergence
    _add_fact(s, "roe_weighted", 3.0)            # ROE < 5 → LowRoe
    _add_fact(s, "revenue", 1000.0)
    _add_fact(s, "rd_expense", 60.0)             # 研发强度 6% → RdIntensive

    out = infer.materialize_defined_classes(s.conn, ax, "run-test")
    assert out["hits"] == 4
    rows = s.conn.execute(
        "SELECT class_id, evidence_fact_ids, values_json, severity FROM inferred_concepts "
        "ORDER BY class_id"
    ).fetchall()
    assert {r[0] for r in rows} == {
        "finkb:CashflowDivergence", "finkb:HighLeverage", "finkb:LowRoe", "finkb:RdIntensive",
    }
    for class_id, fact_ids, values_json, severity in rows:
        assert len(fact_ids) >= 1, f"{class_id} 没有证据事实"
        assert json.loads(values_json), f"{class_id} 没有记录判定时的值"
        assert severity in {"warn", "info"}


def test_defined_class_skips_when_metric_missing(mem_store) -> None:
    s, _doc, ax = mem_store
    _add_fact(s, "total_assets", 1000.0)  # 缺 total_liabilities → HighLeverage 不猜
    out = infer.materialize_defined_classes(s.conn, ax, "run-test")
    assert out["hits"] == 0
    assert out["skipped"]["finkb:HighLeverage"] >= 1


# ---------------------------------------------------------------- 关系边


def test_materialize_edges_inverse_and_chain(mem_store) -> None:
    s, _doc, ax = mem_store
    s.conn.execute(
        "INSERT INTO relations (src_type, src_id, rel_type, dst_type, dst_id, source_doc, page_num, attrs_json, period) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ["company", "000001", "subsidiary_of", "entity", "SUB_A", "000001_2025_年报", 20, None, "2025"],
    )
    s.conn.execute(
        "INSERT INTO relations (src_type, src_id, rel_type, dst_type, dst_id, source_doc, page_num, attrs_json, period) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ["entity", "SUB_A", "guarantees_for", "entity", "GUAR_B", "000001_2025_年报", 21, None, "2025"],
    )
    out = infer.materialize_edges(s.conn, ax, "run-test")
    assert out["base_edges"] == 2
    rows = s.conn.execute(
        "SELECT src_id, rel_type, dst_id, rule_id, premises_json FROM inferred_edges"
    ).fetchall()
    rules = {(r[1], r[3]) for r in rows}
    assert ("finkb:isSubsidiaryOf", "inverse:finkb:hasSubsidiary") in rules
    assert ("finkb:isGuaranteedBy", "inverse:finkb:providesGuaranteeTo") in rules
    chain = [r for r in rows if r[3] == "chain:finkb:chain_indirect_guarantee"]
    assert len(chain) == 1
    assert chain[0][0] == "company:000001" and chain[0][2] == "entity:GUAR_B"
    assert len(json.loads(chain[0][4])) == 2, "属性链必须记录两条前提边"


# ---------------------------------------------------------------- 导出


def test_tbox_has_defined_classes_and_chain(file_store) -> None:
    pytest.importorskip("rdflib")
    from rdflib import OWL, RDF, URIRef

    _s, _db, doc, ax = file_store
    g = export.build_tbox(doc, ax)
    hl = URIRef("https://finkb.local/ontology#HighLeverage")
    assert (hl, RDF.type, OWL.Class) in g
    exprs = list(g.objects(hl, URIRef("https://finkb.local/ontology#definedByExpression")))
    assert exprs and "total_liabilities" in str(exprs[0])
    chains = list(g.subject_objects(OWL.propertyChainAxiom))
    assert chains, "属性链没有导出成 owl:propertyChainAxiom"


def test_abox_has_company_facts_and_provenance(file_store) -> None:
    pytest.importorskip("rdflib")
    from rdflib import OWL, RDF, URIRef

    s, _db, doc, ax = file_store
    _add_fact(s, "revenue", 100.0, company="000001", page=12)
    s.conn.execute(
        "INSERT INTO relations (src_type, src_id, rel_type, dst_type, dst_id, source_doc, page_num, attrs_json, period) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ["company", "000001", "subsidiary_of", "entity", "SUB_A", "000001_2025_年报", 20, None, "2025"],
    )
    g = export.build_abox(s.conn, doc, ax)
    company = URIRef("https://finkb.local/provenance#company/000001")
    assert (company, RDF.type, URIRef("https://finkb.local/ontology#ListedCompany")) in g
    state = URIRef("https://finkb.local/provenance#state/000001/2025")
    metric = URIRef("https://finkb.local/ontology#metric/revenue")
    assert (state, metric, None) in g
    stmts = [s for s in g.subjects(RDF.type, URIRef("https://finkb.local/ontology#RelationStatement"))]
    assert stmts, "关系边缺少 provenance 节点"
    assert (stmts[0], URIRef("https://finkb.local/ontology#pageNum"), None) in g


def test_export_writes_manifest_with_sha(file_store, tmp_path: Path) -> None:
    pytest.importorskip("rdflib")
    s, db, _doc, _ax = file_store
    _add_fact(s, "revenue", 100.0)
    s.close()

    out = tmp_path / "ontology"
    manifest = export.export(db, out)
    for name in ("tbox.ttl", "abox.ttl", "manifest.json"):
        assert (out / name).exists()
    assert manifest["tbox"]["sha256"]
    saved = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert saved["abox"]["sha256"] == manifest["abox"]["sha256"]


# ---------------------------------------------------------------- 推理流水线


def test_run_writes_report_and_tables(file_store, tmp_path: Path) -> None:
    s, db, _doc, _ax = file_store
    _add_fact(s, "total_assets", 1000.0)
    _add_fact(s, "total_liabilities", 800.0)
    s.close()

    out = tmp_path / "ontology"
    report = infer.run(db, out, skip_owl=True)
    assert report["defined_classes"]["hits"] == 1
    assert report["owlrl"] == {"skipped": True}
    assert (out / "inference_report.json").exists()
    assert json.loads((out / "inference_report.json").read_text(encoding="utf-8"))["run_id"] == report["run_id"]

    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM inference_runs").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM inferred_concepts").fetchone()[0] == 1
    finally:
        con.close()


def test_owlrl_closure_filters_trivial_triples(file_store, tmp_path: Path) -> None:
    pytest.importorskip("rdflib")
    pytest.importorskip("owlrl")
    s, db, _doc, _ax = file_store
    _add_fact(s, "revenue", 100.0)
    s.conn.execute(
        "INSERT INTO relations (src_type, src_id, rel_type, dst_type, dst_id, source_doc, page_num, attrs_json, period) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ["company", "000001", "subsidiary_of", "entity", "SUB_A", "000001_2025_年报", 20, None, "2025"],
    )
    s.close()

    out = tmp_path / "ontology"
    report = infer.run(db, out)
    assert report["owlrl"]["inferred_triples"] > 0
    assert (out / "inferred.ttl").exists()

    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    try:
        trivial = con.execute(
            "SELECT count(*) FROM inferred_triples "
            "WHERE (predicate LIKE '%#sameAs' AND subject = object) "
            "   OR (predicate LIKE '%#type' AND object LIKE '%#Thing')"
        ).fetchone()[0]
        assert trivial == 0, "自反 sameAs / owl:Thing 噪音没有过滤"
    finally:
        con.close()


def test_audit_reports_inference_lineage(file_store, tmp_path: Path) -> None:
    """第⑦跳：没跑推理时跳过；跑过之后，缺血缘的推断要能被抓出来。"""
    import duckdb

    from finkb.ontology import audit

    s, db, _doc, _ax = file_store
    _add_fact(s, "total_assets", 1000.0)
    _add_fact(s, "total_liabilities", 800.0)
    assert audit.audit(s.conn)["hops"]["inference"] is None
    s.close()

    infer.run(db, tmp_path / "ontology", skip_owl=True)
    con = duckdb.connect(str(db))
    try:
        assert audit.audit(con)["hops"]["inference"] == 0
        con.execute(
            "INSERT INTO inferred_triples (subject, predicate, object, rule_type, run_id) "
            "VALUES ('a', 'b', 'c', NULL, NULL)"
        )
        assert audit.audit(con)["hops"]["inference"] == 1
    finally:
        con.close()
