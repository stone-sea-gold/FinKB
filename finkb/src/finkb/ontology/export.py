"""把 YAML 本体导出为 OWL 2 RL 可消费的 Turtle（ADR-057）。

分两层导出，和项目的数据分层一一对应：

* **TBox**（``tbox.ttl``）：概念栈 → ``owl:Class`` 与 ``rdfs:subClassOf``；
  公理层 → 对象属性、逆属性、传递/对称特征、``owl:propertyChainAxiom``、
  数值定义类（带判定式与证据指标声明）。
* **ABox**（``abox.ttl``）：公司、财务状态、指标值、担保/持股/关联方/子公司边，
  每条关系边带一个 provenance 节点（doc + page）。

为什么数值定义类只导出表达式、不导出 ``owl:Restriction``：OWL 2 RL 没有算术，
把 ``ocf / net_profit < 0.5`` 硬写成 OWL 公理是**假装**它能推理。这里显式标注
``finkb:ruleKind "datalog-numeric"``，由 ``finkb.ontology.infer`` 用确定性算子求值，
OWL 只负责类层级、属性特征与属性链。

没有 rdflib 时本模块不可用（导出/推理是可选能力，基础流水线不依赖它）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from finkb.common import paths
from urllib.parse import quote

import duckdb
import yaml

from finkb.ontology import model

try:  # pragma: no cover - 缺依赖时由调用方给出清晰提示
    from rdflib import BNode, Graph, Literal, Namespace, RDF, RDFS, OWL, XSD, URIRef
    from rdflib.collection import Collection
except ImportError:  # pragma: no cover
    BNode = Graph = Literal = Namespace = RDF = RDFS = OWL = XSD = URIRef = None
    Collection = None

FINKB = "https://finkb.local/ontology#"
PROV = "https://finkb.local/provenance#"
FINKB_NS = Namespace(FINKB)
PROV_NS = Namespace(PROV)

REL_TO_PROPERTY = {
    "guarantees_for": "finkb:providesGuaranteeTo",
    "subsidiary_of": "finkb:hasSubsidiary",
    "shareholder_of": "finkb:holdsEquityIn",
    "related_party_of": "finkb:relatedPartyOf",
}


class ExportError(RuntimeError):
    pass


def _require_rdflib() -> None:
    if Graph is None:
        raise ExportError("未安装 rdflib（pip install rdflib owlrl），无法导出 OWL")


def _local(iri: str) -> str:
    """``finkb:hasSubsidiary`` → ``hasSubsidiary``；其它前缀保留冒号。"""
    if iri.startswith("finkb:"):
        return iri.split(":", 1)[1]
    return iri


def _concept_uri(concept: model.Concept) -> URIRef:
    if concept.basis_uri:
        return URIRef(concept.basis_uri)
    return URIRef(FINKB + _local(concept.concept_id))


def _axiom_uri(class_id: str) -> URIRef:
    return URIRef(FINKB + _local(class_id))


def _property_uri(property_id: str) -> URIRef:
    return URIRef(FINKB + _local(property_id))


def _metric_uri(metric_id: str) -> URIRef:
    return URIRef(FINKB + "metric/" + metric_id)


def _state_uri(company: str, period: str) -> URIRef:
    return URIRef(f"{PROV}state/{quote(company)}/{quote(period)}")


def _company_uri(company: str) -> URIRef:
    return URIRef(f"{PROV}company/{quote(company)}")


def _entity_uri(dst_id: str) -> URIRef:
    return URIRef(f"{PROV}entity/{quote(dst_id)}")


def build_tbox(doc: model.OntologyDoc, ax: model.AxiomDoc) -> "Graph":
    """TBox：概念层级 + 对象属性 + 属性链 + 数值定义类。"""
    _require_rdflib()
    g = Graph()
    g.bind("finkb", FINKB_NS)
    g.bind("prov", PROV_NS)

    ontology = URIRef(FINKB.rstrip("#"))
    g.add((ontology, RDF.type, OWL.Ontology))
    g.add((ontology, URIRef(FINKB + "version"), Literal(doc.version)))
    g.add((ontology, RDFS.comment, Literal("FinKB 本体：概念栈 + 公理层（ADR-057）", lang="zh")))

    concept_uris: dict[str, URIRef] = {}
    for c in doc.concepts:
        u = _concept_uri(c)
        concept_uris[c.concept_id] = u
        g.add((u, RDF.type, OWL.Class))
        g.add((u, RDFS.label, Literal(c.label, lang="zh")))
        if c.definition:
            g.add((u, RDFS.comment, Literal(c.definition, lang="zh")))
        if c.basis_uri:
            g.add((u, URIRef(FINKB + "basisUri"), Literal(c.basis_uri)))
        if c.parent_id and c.parent_id in concept_uris:
            g.add((u, RDFS.subClassOf, concept_uris[c.parent_id]))
        elif c.parent_id:
            g.add((u, RDFS.subClassOf, URIRef(FINKB + _local(c.parent_id))))
        if c.formula:
            g.add((u, URIRef(FINKB + "formula"), Literal(c.formula)))
        if c.metric_id:
            g.add((u, URIRef(FINKB + "metricId"), Literal(c.metric_id)))

    # 概念间关系：is_a 已是 subClassOf；derived_from 保留为注解边（非推理语义）
    for r in doc.relations:
        if r.rel_type == "is_a" and r.src_concept in concept_uris and r.dst_concept in concept_uris:
            g.add((concept_uris[r.src_concept], RDFS.subClassOf, concept_uris[r.dst_concept]))
        elif r.rel_type == "disjoint_with":
            if r.src_concept in concept_uris and r.dst_concept in concept_uris:
                g.add((concept_uris[r.src_concept], OWL.disjointWith, concept_uris[r.dst_concept]))
        elif r.rel_type == "derived_from":
            if r.src_concept in concept_uris and r.dst_concept in concept_uris:
                g.add((concept_uris[r.src_concept], URIRef(FINKB + "derivedFrom"),
                       concept_uris[r.dst_concept]))
        elif r.rel_type == "part_of":
            if r.src_concept in concept_uris and r.dst_concept in concept_uris:
                g.add((concept_uris[r.src_concept], URIRef(FINKB + "partOf"),
                       concept_uris[r.dst_concept]))
        elif r.rel_type == "equivalent_to":
            if r.src_concept in concept_uris and r.dst_concept in concept_uris:
                g.add((concept_uris[r.src_concept], OWL.equivalentClass,
                       concept_uris[r.dst_concept]))

    # 公理层：类
    for c in ax.classes:
        u = _axiom_uri(c.class_id)
        g.add((u, RDF.type, OWL.Class))
        g.add((u, RDFS.label, Literal(c.label, lang="zh")))
        if c.comment:
            g.add((u, RDFS.comment, Literal(c.comment, lang="zh")))
        if c.parent:
            parent_uri = concept_uris.get(c.parent) or _axiom_uri(c.parent)
            g.add((u, RDFS.subClassOf, parent_uri))

    # 公理层：对象属性
    for p in ax.object_properties:
        u = _property_uri(p.property_id)
        g.add((u, RDF.type, OWL.ObjectProperty))
        g.add((u, RDFS.label, Literal(p.label, lang="zh")))
        if p.comment:
            g.add((u, RDFS.comment, Literal(p.comment, lang="zh")))
        if p.domain:
            g.add((u, RDFS.domain, concept_uris.get(p.domain) or _axiom_uri(p.domain)))
        if p.range:
            g.add((u, RDFS.range, concept_uris.get(p.range) or _axiom_uri(p.range)))
        if p.inverse_of:
            g.add((u, OWL.inverseOf, _property_uri(p.inverse_of)))
        for ch in p.characteristics:
            if ch == "transitive":
                g.add((u, RDF.type, OWL.TransitiveProperty))
            elif ch == "symmetric":
                g.add((u, RDF.type, OWL.SymmetricProperty))
            elif ch == "functional":
                g.add((u, RDF.type, OWL.FunctionalProperty))
            elif ch == "inverse_functional":
                g.add((u, RDF.type, OWL.InverseFunctionalProperty))
            elif ch == "reflexive":
                g.add((u, RDF.type, OWL.ReflexiveProperty))
            elif ch == "irreflexive":
                g.add((u, RDF.type, OWL.IrreflexiveProperty))
            elif ch == "asymmetric":
                g.add((u, RDF.type, OWL.AsymmetricProperty))

    # 公理层：属性链
    for ch in ax.property_chains:
        implies = _property_uri(ch.implies)
        g.add((implies, RDF.type, OWL.ObjectProperty))
        if ch.comment:
            g.add((implies, RDFS.comment, Literal(ch.comment, lang="zh")))
        head = BNode()
        Collection(g, head, [_property_uri(pid) for pid in ch.properties])
        g.add((implies, OWL.propertyChainAxiom, head))

    # 公理层：数值定义类（Datalog 规则，不是 OWL Restriction）
    for d in ax.defined_classes:
        u = _axiom_uri(d.class_id)
        g.add((u, RDF.type, OWL.Class))
        g.add((u, RDFS.label, Literal(d.label, lang="zh")))
        if d.definition:
            g.add((u, RDFS.comment, Literal(d.definition, lang="zh")))
        if d.parent:
            g.add((u, RDFS.subClassOf, concept_uris.get(d.parent) or _axiom_uri(d.parent)))
        g.add((u, URIRef(FINKB + "definedByExpression"), Literal(d.expression)))
        g.add((u, URIRef(FINKB + "ruleKind"), Literal("datalog-numeric")))
        for m in d.evidence_metrics:
            g.add((u, URIRef(FINKB + "evidenceMetric"), Literal(m)))

    # 指标 → 数据属性（ABox 用）
    for c in doc.concepts:
        if not c.metric_id:
            continue
        g.add((_metric_uri(c.metric_id), RDF.type, OWL.DatatypeProperty))
        g.add((_metric_uri(c.metric_id), RDFS.label, Literal(c.label, lang="zh")))
        g.add((_metric_uri(c.metric_id), RDFS.domain, _axiom_uri("finkb:FinancialState")))
        g.add((_metric_uri(c.metric_id), RDFS.range, XSD.decimal))
    return g


def _scope_names(path: str | Path | None = None) -> dict[str, str]:
    """公司代码 → 公司简称（来自 wiki_scope.yaml；缺文件不影响导出）。"""
    p = Path(path) if path else paths.config_dir() / "wiki_scope.yaml"
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {
        str(d.get("company")): str(d.get("company_name") or d.get("company"))
        for d in raw.get("docs") or []
        if d.get("company")
    }


def build_abox(
    conn: duckdb.DuckDBPyConnection,
    doc: model.OntologyDoc,
    ax: model.AxiomDoc,
    *,
    scope_path: str | Path | None = None,
    with_relation_provenance: bool = True,
) -> "Graph":
    """ABox：公司、财务状态、指标值、关系边（带 doc/page 来源）。"""
    _require_rdflib()
    g = Graph()
    g.bind("finkb", FINKB_NS)
    g.bind("prov", PROV_NS)
    names = _scope_names(scope_path)
    company_type = _axiom_uri("finkb:ListedCompany")
    state_type = _axiom_uri("finkb:FinancialState")
    has_state = _property_uri("finkb:hasFinancialState")

    for (company,) in conn.execute("SELECT DISTINCT company FROM facts_dedup ORDER BY 1").fetchall():
        cu = _company_uri(company)
        g.add((cu, RDF.type, company_type))
        g.add((cu, RDFS.label, Literal(names.get(company, company), lang="zh")))

    for company, period in conn.execute(
        "SELECT DISTINCT company, period FROM facts_dedup ORDER BY 1, 2"
    ).fetchall():
        su = _state_uri(company, period)
        g.add((su, RDF.type, state_type))
        g.add((su, RDFS.label, Literal(f"{names.get(company, company)} {period}", lang="zh")))
        g.add((_company_uri(company), has_state, su))

    for company, period, metric_id, value in conn.execute(
        "SELECT company, period, metric_id, value_norm FROM facts_dedup ORDER BY 1,2,3"
    ).fetchall():
        if value is None:
            continue
        g.add((_state_uri(company, period), _metric_uri(metric_id),
               Literal(float(value), datatype=XSD.decimal)))

    rows = conn.execute(
        "SELECT src_type, src_id, rel_type, dst_type, dst_id, source_doc, page_num "
        "FROM relations ORDER BY src_id, rel_type, dst_id, source_doc, page_num"
    ).fetchall()
    for src_type, src_id, rel_type, dst_type, dst_id, source_doc, page_num in rows:
        prop = REL_TO_PROPERTY.get(rel_type)
        if prop is None:
            continue
        if src_type == "company":
            subject = _company_uri(src_id)
        else:
            subject = _entity_uri(src_id)
        obj = _entity_uri(dst_id)
        g.add((subject, _property_uri(prop), obj))
        if dst_type == "person":
            g.add((obj, RDF.type, _axiom_uri("finkb:Person")))
        else:
            g.add((obj, RDF.type, _axiom_uri("finkb:Entity")))
        if with_relation_provenance:
            stmt = BNode()
            g.add((stmt, RDF.type, URIRef(FINKB + "RelationStatement")))
            g.add((stmt, URIRef(FINKB + "subject"), subject))
            g.add((stmt, URIRef(FINKB + "predicate"), _property_uri(prop)))
            g.add((stmt, URIRef(FINKB + "object"), obj))
            if source_doc:
                g.add((stmt, URIRef(FINKB + "sourceDoc"), Literal(source_doc)))
            if page_num is not None:
                g.add((stmt, URIRef(FINKB + "pageNum"), Literal(int(page_num))))
    return g


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(
    db_path: str | Path,
    out_dir: str | Path,
    *,
    ontology_path: str | Path | None = None,
    axioms_path: str | Path | None = None,
    with_abox: bool = True,
    scope_path: str | Path | None = None,
) -> dict[str, Any]:
    """导出 TBox/ABox 到 ``out_dir``，返回 manifest。"""
    _require_rdflib()
    doc = model.load(ontology_path)
    ax = model.load_axioms(axioms_path, doc=doc)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    tbox = build_tbox(doc, ax)
    manifest: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ontology_version": doc.version,
        "axioms_version": ax.version,
        "tbox": {"triples": len(tbox), "path": "tbox.ttl"},
        "abox": None,
        "warnings": list(ax.warnings),
    }
    tbox_path = out / "tbox.ttl"
    _atomic_write(tbox_path, tbox.serialize(format="turtle"))
    manifest["tbox"]["sha256"] = _sha256(tbox_path)

    if with_abox:
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            abox = build_abox(conn, doc, ax, scope_path=scope_path)
        finally:
            conn.close()
        abox_path = out / "abox.ttl"
        _atomic_write(abox_path, abox.serialize(format="turtle"))
        manifest["abox"] = {
            "triples": len(abox),
            "path": "abox.ttl",
            "sha256": _sha256(abox_path),
        }

    _atomic_write(out / "manifest.json",
                  json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
