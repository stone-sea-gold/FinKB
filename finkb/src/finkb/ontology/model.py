"""本体层的数据模型与 YAML 载入。

约定：``config/ontology.yaml`` 是**单一真相源**。本模块只读它、校验它，
不负责生成（生成见 ``tools/build_ontology.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 合法的层与关系类型。relation 用开放字符串，但校验时只警告不报错——
# 扩展新关系类型不应该需要改代码。
LAYERS = ("L0", "L1", "L2", "L3")
KNOWN_REL_TYPES = {"is_a", "derived_from", "equivalent_to", "disjoint_with", "part_of"}
RULE_TYPES = {"acct_equation", "range", "required_period", "unit_check"}


@dataclass
class Concept:
    concept_id: str
    label: str
    layer: str
    domain: str = "finance"
    basis: str | None = None
    basis_uri: str | None = None
    basis_element: str | None = None
    definition: str | None = None
    parent_id: str | None = None
    metric_id: str | None = None
    unit_default: str | None = None
    formula: str | None = None
    expression_args: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    source_uri: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None

    @property
    def is_derived(self) -> bool:
        return bool(self.formula)


@dataclass
class Relation:
    src_concept: str
    rel_type: str
    dst_concept: str
    attrs_json: str | None = None
    source_uri: str | None = None


@dataclass
class Constraint:
    constraint_id: str
    concept_id: str
    rule_type: str
    expression: str
    tolerance: float | None = None
    severity: str = "error"
    note: str | None = None
    source_uri: str | None = None


@dataclass
class Dimension:
    """维度种类（ADR-054）。

    维度**种类**是封闭词表：由准则与披露实践决定，不随年报变。维度**成员**
    （分产品下的「新能源汽车」等）是数据驱动的开放集合，落在
    ``dimension_members`` 里，由结构表行名自动抽取。二者分开，是「新增一份年报
    不需要任何维度配置」的前提。
    """

    dim_kind: str
    label: str
    markers: str = ""
    concept_id: str | None = None
    note: str | None = None


# ---------------------------------------------------------------- 公理层
# 公理层（ontology_axioms.yaml）与概念层（ontology.yaml）分开存放：
# 前者手工维护类/属性/属性链/数值规则，后者由 tools/build_ontology.py 生成。
AXIOM_CHARACTERISTICS = {
    "transitive", "symmetric", "functional", "inverse_functional",
    "reflexive", "irreflexive", "asymmetric",
}
DEFINED_CLASS_SEVERITIES = {"error", "warn", "info"}


@dataclass
class AxiomClass:
    class_id: str
    label: str
    parent: str | None = None
    comment: str | None = None


@dataclass
class ObjectProperty:
    property_id: str
    label: str
    domain: str | None = None
    range: str | None = None
    inverse_of: str | None = None
    characteristics: list[str] = field(default_factory=list)
    comment: str | None = None


@dataclass
class PropertyChain:
    chain_id: str
    properties: list[str]
    implies: str
    comment: str | None = None


@dataclass
class DefinedClass:
    class_id: str
    label: str
    parent: str | None
    expression: str
    definition: str | None = None
    note: str | None = None
    severity: str = "warn"
    evidence_metrics: list[str] = field(default_factory=list)

    @property
    def metrics(self) -> list[str]:
        return list(self.evidence_metrics)


@dataclass
class AxiomDoc:
    version: int
    namespace: str
    classes: list[AxiomClass] = field(default_factory=list)
    object_properties: list[ObjectProperty] = field(default_factory=list)
    property_chains: list[PropertyChain] = field(default_factory=list)
    defined_classes: list[DefinedClass] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def class_by_id(self, class_id: str) -> AxiomClass | None:
        for c in self.classes:
            if c.class_id == class_id:
                return c
        return None

    def property_by_id(self, property_id: str) -> ObjectProperty | None:
        for p in self.object_properties:
            if p.property_id == property_id:
                return p
        return None

    def defined_by_id(self, class_id: str) -> DefinedClass | None:
        for d in self.defined_classes:
            if d.class_id == class_id:
                return d
        return None

    def class_ids(self) -> set[str]:
        return {c.class_id for c in self.classes} | {d.class_id for d in self.defined_classes}



@dataclass
class OntologyDoc:
    version: int
    concepts: list[Concept]
    relations: list[Relation]
    constraints: list[Constraint]
    taxonomy: dict[str, Any] = field(default_factory=dict)
    layers: dict[str, str] = field(default_factory=dict)
    dimensions: list[Dimension] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_id(self, concept_id: str) -> Concept | None:
        for c in self.concepts:
            if c.concept_id == concept_id:
                return c
        return None

    def dimension(self, dim_kind: str) -> Dimension | None:
        for d in self.dimensions:
            if d.dim_kind == dim_kind:
                return d
        return None

    def by_metric(self, metric_id: str) -> Concept | None:
        for c in self.concepts:
            if c.metric_id == metric_id:
                return c
        return None

    def children(self, concept_id: str) -> list[Concept]:
        return [c for c in self.concepts if c.parent_id == concept_id]

    def constraints_of(self, concept_id: str) -> list[Constraint]:
        """概念自身的约束 + 祖先的约束（约束沿 is_a 继承）。"""
        ids = {concept_id}
        cur = self.by_id(concept_id)
        seen: set[str] = set()
        while cur and cur.parent_id and cur.parent_id not in seen:
            seen.add(cur.parent_id)
            ids.add(cur.parent_id)
            cur = self.by_id(cur.parent_id)
        return [c for c in self.constraints if c.concept_id in ids]

    def ancestry(self, concept_id: str) -> list[Concept]:
        """从自身一路到根的祖先链（自身在前）。"""
        out: list[Concept] = []
        cur = self.by_id(concept_id)
        seen: set[str] = set()
        while cur and cur.concept_id not in seen:
            seen.add(cur.concept_id)
            out.append(cur)
            cur = self.by_id(cur.parent_id) if cur.parent_id else None
        return out


def default_path() -> Path:
    """``finkb/config/ontology.yaml``（相对包根往上一级）。"""
    return Path(__file__).resolve().parents[3] / "config" / "ontology.yaml"


def _s(v: Any) -> str | None:
    return None if v in (None, "") else str(v)


def load(path: str | Path | None = None) -> OntologyDoc:
    """载入并校验本体。校验失败抛 ``ValueError``；可疑但不致命进 ``warnings``。"""
    p = Path(path) if path else default_path()
    if not p.exists():
        raise FileNotFoundError(f"本体文件不存在：{p}（先跑 tools/build_ontology.py）")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    concepts = [
        Concept(
            concept_id=str(c["concept_id"]),
            label=str(c["label"]),
            layer=str(c.get("layer", "L2")),
            domain=str(c.get("domain", "finance")),
            basis=_s(c.get("basis")),
            basis_uri=_s(c.get("basis_uri")),
            basis_element=_s(c.get("basis_element")),
            definition=_s(c.get("definition")),
            parent_id=_s(c.get("parent_id")),
            metric_id=_s(c.get("metric_id")),
            unit_default=_s(c.get("unit_default")),
            formula=_s(c.get("formula")),
            expression_args=list(c.get("expression_args") or []),
            aliases=[str(a) for a in (c.get("aliases") or [])],
            source_uri=_s(c.get("source_uri")),
            valid_from=_s(c.get("valid_from")),
            valid_to=_s(c.get("valid_to")),
        )
        for c in raw.get("concepts") or []
    ]
    relations = [
        Relation(
            src_concept=str(r["src_concept"]),
            rel_type=str(r["rel_type"]),
            dst_concept=str(r["dst_concept"]),
            attrs_json=_s(r.get("attrs_json")),
            source_uri=_s(r.get("source_uri")),
        )
        for r in raw.get("relations") or []
    ]
    constraints = [
        Constraint(
            constraint_id=str(c["constraint_id"]),
            concept_id=str(c["concept_id"]),
            rule_type=str(c["rule_type"]),
            expression=str(c["expression"]),
            tolerance=c.get("tolerance"),
            severity=str(c.get("severity") or "error"),
            note=_s(c.get("note")),
            source_uri=_s(c.get("source_uri")),
        )
        for c in raw.get("constraints") or []
    ]

    dimensions = [
        Dimension(
            dim_kind=str(d["dim_kind"]),
            label=str(d["label"]),
            markers=str(d.get("markers") or ""),
            concept_id=_s(d.get("concept_id")),
            note=_s(d.get("note")),
        )
        for d in raw.get("dimensions") or []
    ]

    doc = OntologyDoc(
        version=int(raw.get("version") or 0),
        concepts=concepts,
        relations=relations,
        constraints=constraints,
        taxonomy=raw.get("taxonomy") or {},
        layers=raw.get("layers") or {},
        dimensions=dimensions,
    )
    doc.warnings = _validate(doc)
    return doc


def axioms_path() -> Path:
    """``finkb/config/ontology_axioms.yaml``（公理层，手工维护）。"""
    return Path(__file__).resolve().parents[3] / "config" / "ontology_axioms.yaml"


def load_axioms(
    path: str | Path | None = None,
    doc: OntologyDoc | None = None,
) -> AxiomDoc:
    """载入并校验公理层。文件不存在时返回空公理并给 warning（不阻塞老流程）。"""
    from finkb.ontology import expr  # 局部导入，避免 model/expr 的循环依赖误伤

    p = Path(path) if path else axioms_path()
    if not p.exists():
        return AxiomDoc(version=0, namespace="", warnings=[f"公理文件不存在：{p}（跳过）"])
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    doc = doc or load()

    classes = [
        AxiomClass(
            class_id=str(c["class_id"]),
            label=str(c["label"]),
            parent=_s(c.get("parent")),
            comment=_s(c.get("comment")),
        )
        for c in raw.get("classes") or []
    ]
    object_properties = [
        ObjectProperty(
            property_id=str(p_["property_id"]),
            label=str(p_["label"]),
            domain=_s(p_.get("domain")),
            range=_s(p_.get("range")),
            inverse_of=_s(p_.get("inverse_of")),
            characteristics=[str(x) for x in (p_.get("characteristics") or [])],
            comment=_s(p_.get("comment")),
        )
        for p_ in raw.get("object_properties") or []
    ]
    property_chains = [
        PropertyChain(
            chain_id=str(c["chain_id"]),
            properties=[str(x) for x in (c.get("properties") or [])],
            implies=str(c["implies"]),
            comment=_s(c.get("comment")),
        )
        for c in raw.get("property_chains") or []
    ]
    defined_classes = []
    for d in raw.get("defined_classes") or []:
        deps = sorted(expr.condition_vars(str(d["expression"])))
        evidence = [str(x) for x in (d.get("evidence_metrics") or [])]
        defined_classes.append(
            DefinedClass(
                class_id=str(d["class_id"]),
                label=str(d["label"]),
                parent=_s(d.get("parent")),
                expression=str(d["expression"]),
                definition=_s(d.get("definition")),
                note=_s(d.get("note")),
                severity=str(d.get("severity") or "warn"),
                evidence_metrics=evidence or deps,
            )
        )

    ax = AxiomDoc(
        version=int(raw.get("version") or 0),
        namespace=str(raw.get("namespace") or "https://finkb.local/ontology#"),
        classes=classes,
        object_properties=object_properties,
        property_chains=property_chains,
        defined_classes=defined_classes,
    )
    ax.warnings = _validate_axioms(ax, doc)
    return ax


def _validate_axioms(ax: AxiomDoc, doc: OntologyDoc) -> list[str]:
    from finkb.ontology import expr  # 局部导入，避免 model/expr 循环依赖

    warns: list[str] = []
    concept_ids = {c.concept_id for c in doc.concepts}
    metric_ids = {c.metric_id for c in doc.concepts if c.metric_id}
    class_ids = [c.class_id for c in ax.classes]
    defined_ids = [d.class_id for d in ax.defined_classes]

    for label, ids in (("class", class_ids), ("defined_class", defined_ids)):
        dups = {i for i in ids if ids.count(i) > 1}
        if dups:
            raise ValueError(f"{label} 重复：{sorted(dups)}")
    clash = set(class_ids) & concept_ids
    if clash:
        raise ValueError(f"公理类与概念层重名：{sorted(clash)}")
    clash = set(defined_ids) & (concept_ids | set(class_ids))
    if clash:
        raise ValueError(f"数值定义类与已有类/概念重名：{sorted(clash)}")

    all_class_ids = concept_ids | set(class_ids) | set(defined_ids)
    parent_of = {c.concept_id: c.parent_id for c in doc.concepts}
    parent_of.update({c.class_id: c.parent for c in ax.classes})
    parent_of.update({d.class_id: d.parent for d in ax.defined_classes})

    for c in ax.classes + ax.defined_classes:
        if c.parent and c.parent not in all_class_ids:
            raise ValueError(f"{c.class_id}: parent {c.parent} 不存在")
    # 父链环检测
    for cid in list(parent_of):
        seen, cur = set(), cid
        while cur and parent_of.get(cur):
            if cur in seen:
                raise ValueError(f"父链成环：{cid}")
            seen.add(cur)
            cur = parent_of[cur]

    prop_ids = [p.property_id for p in ax.object_properties]
    dups = {i for i in prop_ids if prop_ids.count(i) > 1}
    if dups:
        raise ValueError(f"对象属性重复：{sorted(dups)}")
    for p in ax.object_properties:
        for field_name, ref in (("domain", p.domain), ("range", p.range)):
            if ref and ref not in all_class_ids:
                raise ValueError(f"{p.property_id}: {field_name} {ref} 不存在")
        if p.inverse_of and p.inverse_of not in prop_ids:
            raise ValueError(f"{p.property_id}: inverse_of {p.inverse_of} 不存在")
        bad = set(p.characteristics) - AXIOM_CHARACTERISTICS
        if bad:
            raise ValueError(f"{p.property_id}: 非法 characteristics {sorted(bad)}")

    for ch in ax.property_chains:
        if not ch.properties:
            raise ValueError(f"{ch.chain_id}: properties 为空")
        for pid in ch.properties:
            if pid not in prop_ids:
                raise ValueError(f"{ch.chain_id}: 属性 {pid} 不存在")
        if ch.implies not in prop_ids:
            raise ValueError(f"{ch.chain_id}: implies {ch.implies} 不是已声明对象属性")

    for d in ax.defined_classes:
        try:
            deps = expr.condition_vars(d.expression)
        except expr.ExprError as e:
            raise ValueError(f"{d.class_id}: 表达式无法解析：{e}") from e
        unknown = sorted(deps - metric_ids)
        if unknown:
            raise ValueError(f"{d.class_id}: 表达式引用未知指标 {unknown}")
        if d.evidence_metrics and set(d.evidence_metrics) != deps:
            warns.append(
                f"{d.class_id}: evidence_metrics {d.evidence_metrics} 与表达式依赖 {sorted(deps)} 不一致"
            )
        if d.severity not in DEFINED_CLASS_SEVERITIES:
            raise ValueError(f"{d.class_id}: 非法 severity {d.severity}")
    return warns


def _validate(doc: OntologyDoc) -> list[str]:
    warns: list[str] = []
    ids = [c.concept_id for c in doc.concepts]
    dups = {i for i in ids if ids.count(i) > 1}
    if dups:
        raise ValueError(f"concept_id 重复：{sorted(dups)}")
    idset = set(ids)

    for c in doc.concepts:
        if c.layer not in LAYERS:
            raise ValueError(f"{c.concept_id}: 非法 layer {c.layer}")
        if c.parent_id and c.parent_id not in idset:
            raise ValueError(f"{c.concept_id}: parent_id {c.parent_id} 不存在")
        if c.layer in ("L0", "L1") and c.metric_id:
            warns.append(f"{c.concept_id}: 元概念/骨架层不应直接挂 metric_id")
        if c.layer == "L3" and not c.formula:
            warns.append(f"{c.concept_id}: L3 派生指标缺 formula")
        if c.formula:
            unknown = [a for a in c.expression_args if a not in {x.metric_id for x in doc.concepts}]
            if unknown:
                warns.append(f"{c.concept_id}: expression_args 里有未定义指标 {unknown}")

    for r in doc.relations:
        if r.src_concept not in idset or r.dst_concept not in idset:
            raise ValueError(f"relation 端点不存在：{r.src_concept} -> {r.dst_concept}")
        if r.rel_type not in KNOWN_REL_TYPES:
            warns.append(f"未知关系类型 {r.rel_type}（允许，但下游可能不认）")

    for con in doc.constraints:
        if con.concept_id not in idset:
            raise ValueError(f"{con.constraint_id}: concept {con.concept_id} 不存在")
        if con.rule_type not in RULE_TYPES:
            raise ValueError(f"{con.constraint_id}: 非法 rule_type {con.rule_type}")

    kinds = [d.dim_kind for d in doc.dimensions]
    if len(kinds) != len(set(kinds)):
        raise ValueError(f"dim_kind 重复：{sorted({k for k in kinds if kinds.count(k) > 1})}")
    for d in doc.dimensions:
        if d.concept_id and d.concept_id not in idset:
            raise ValueError(f"维度 {d.dim_kind}: concept {d.concept_id} 不存在")

    # 环检测：parent 链不能成环
    for c in doc.concepts:
        seen, cur = set(), c
        while cur and cur.parent_id:
            if cur.concept_id in seen:
                raise ValueError(f"parent 链成环：{c.concept_id}")
            seen.add(cur.concept_id)
            cur = doc.by_id(cur.parent_id)
    return warns
