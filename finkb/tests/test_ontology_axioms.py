"""公理层（类 / 对象属性 / 属性链 / 数值定义类）与布尔条件表达式的离线测试。

守两件事：
1. 公理文件可以被校验器读进来，引用不悬空（父类、逆属性、属性链、指标名）。
2. ``expr`` 能解析并求值 ``and/or/not`` 与链式比较——这是 v3.1 数值定义类的前提。
"""

from __future__ import annotations

import pytest

from finkb.fact_engine.metric_dict import load_metric_dict
from finkb.ontology import expr, model


@pytest.fixture(scope="module")
def doc() -> model.OntologyDoc:
    return model.load()


@pytest.fixture(scope="module")
def ax(doc: model.OntologyDoc) -> model.AxiomDoc:
    return model.load_axioms(doc=doc)


# ---------------------------------------------------------------- 公理文件


def test_axioms_load_without_warnings(ax: model.AxiomDoc) -> None:
    assert ax.warnings == []
    assert ax.version >= 1
    assert len(ax.classes) == 7
    assert len(ax.object_properties) == 10
    assert len(ax.property_chains) == 1
    assert len(ax.defined_classes) == 8


def test_class_parents_resolve(ax: model.AxiomDoc, doc: model.OntologyDoc) -> None:
    known = {c.concept_id for c in doc.concepts} | {c.class_id for c in ax.classes}
    for c in ax.classes:
        if c.parent is not None:
            assert c.parent in known, f"{c.class_id} 的父类 {c.parent} 不存在"


def test_inverse_properties_are_reciprocal(ax: model.AxiomDoc) -> None:
    by_id = {p.property_id: p for p in ax.object_properties}
    for p in ax.object_properties:
        if p.inverse_of:
            assert p.inverse_of in by_id
            assert by_id[p.inverse_of].inverse_of == p.property_id


def test_property_chain_edges_exist(ax: model.AxiomDoc) -> None:
    props = {p.property_id for p in ax.object_properties}
    for ch in ax.property_chains:
        assert len(ch.properties) >= 2
        assert set(ch.properties) <= props
        assert ch.implies in props


def test_defined_classes_reference_known_metrics(ax: model.AxiomDoc) -> None:
    metrics = {m.metric_id for m in load_metric_dict()}
    for d in ax.defined_classes:
        deps = expr.condition_deps(expr.parse_condition(d.expression))
        assert deps, f"{d.class_id} 没有依赖任何指标"
        assert deps <= metrics, f"{d.class_id} 引用了字典外指标：{deps - metrics}"


# ---------------------------------------------------------------- 条件表达式


def test_condition_and_or_not() -> None:
    node = expr.parse_condition("a > 0 and (b < 0 or c > 1)")
    assert expr.condition_deps(node) == {"a", "b", "c"}
    assert expr.evaluate_condition(node, {"a": 1, "b": 1, "c": 2}) is True
    assert expr.evaluate_condition(node, {"a": 1, "b": 1, "c": 0}) is False
    assert expr.evaluate_condition(node, {"a": 0, "b": -1, "c": 0}) is False


def test_condition_not() -> None:
    node = expr.parse_condition("not (x > 1)")
    assert expr.evaluate_condition(node, {"x": 0}) is True
    assert expr.evaluate_condition(node, {"x": 2}) is False


def test_condition_chain_comparison() -> None:
    node = expr.parse_condition("0 < x <= 1")
    assert expr.evaluate_condition(node, {"x": 0.5}) is True
    assert expr.evaluate_condition(node, {"x": 1.0}) is True
    assert expr.evaluate_condition(node, {"x": 1.5}) is False


def test_condition_arithmetic_ratio() -> None:
    node = expr.parse_condition("net_profit > 0 and ocf / net_profit < 0.5")
    assert expr.evaluate_condition(node, {"net_profit": 100, "ocf": 10}) is True
    assert expr.evaluate_condition(node, {"net_profit": 100, "ocf": 80}) is False
    assert expr.evaluate_condition(node, {"net_profit": -5, "ocf": 10}) is False


def test_condition_rejects_garbage() -> None:
    with pytest.raises(expr.ExprError):
        expr.parse_condition("a > 1 and")
    with pytest.raises(expr.ExprError):
        expr.parse_condition("")
