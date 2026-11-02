"""L0.5 本体层：概念栈 + 可追溯链条。

回答的问题：这个指标**是什么类型的东西**（元概念）、**在报表里叫什么**（国标科目）、
**和谁有约束关系**（会计等式）、**这个数字从哪页来**（证据）。

设计要点见 ``FinKB-方案v2.md`` §7.5；硬边界：
- 不引入 OWL/RDF 运行时，不引图数据库（``concept_relations`` 是普通边表）
- 只做校验不做推导（:mod:`finkb.ontology.check` 只报错，不修数）
- 概念是**数据**不是代码：扩科目 = 往 ``config/ontology.yaml`` 加行，不改 schema

模块：
    model      载入与校验 ontology.yaml
    schema     DuckDB 表结构
    loader     YAML → DuckDB（幂等）
    chain      六跳链条解析（本体 → 原文）
    check      约束校验
    derive     L3 派生指标计算
    expr       无 eval 的算术表达式求值
"""

from __future__ import annotations

from finkb.ontology.model import Concept, Constraint, OntologyDoc, Relation, load

__all__ = ["Concept", "Constraint", "OntologyDoc", "Relation", "load_ontology"]


def load_ontology(path: str | None = None) -> OntologyDoc:
    """载入 ``config/ontology.yaml``（不做任何 DB 操作）。"""
    return load(path)
