"""本体层的表结构（与 :mod:`finkb.fact_engine.store` 共用同一个 DuckDB 文件）。

为什么放在同一个库里而不是单独建库：链条的第 4–6 跳（概念 → 事实 → 证据 → 页面）
需要 join ``facts`` / ``claims``。分库就得在 Python 里做内存 join，既慢又容易漏事实。
同库一条 SQL 就能把「本体 → 原文」走完。

表名说明：``constraints`` 是 SQL 关键字，故表名用 ``ontology_constraints``
（方案 v2.3 §7.5.3 的记法）。
"""

from __future__ import annotations

ONTOLOGY_SQL = """
-- 概念：四层栈的节点。layer 决定它是元概念还是科目，domain 预留扩展域。
CREATE TABLE IF NOT EXISTS concepts (
  concept_id      VARCHAR PRIMARY KEY,
  label           VARCHAR NOT NULL,
  layer           VARCHAR NOT NULL,
  domain          VARCHAR NOT NULL,
  basis           VARCHAR,
  basis_uri       VARCHAR,
  basis_element   VARCHAR,
  definition      TEXT,
  parent_id       VARCHAR,
  metric_id       VARCHAR,
  unit_default    VARCHAR,
  formula         VARCHAR,
  expression_args VARCHAR[],
  aliases         VARCHAR[],
  source_uri      VARCHAR,
  valid_from      VARCHAR,
  valid_to        VARCHAR
);

-- 概念间关系：通用边表。rel_type 是开放字符串（is_a / derived_from /
-- equivalent_to / disjoint_with / part_of），新增关系类型不需要改表结构。
CREATE TABLE IF NOT EXISTS concept_relations (
  src_concept VARCHAR NOT NULL,
  rel_type    VARCHAR NOT NULL,
  dst_concept VARCHAR NOT NULL,
  attrs_json  VARCHAR,
  source_uri  VARCHAR
);

-- 约束：只做校验，不做推导。
CREATE TABLE IF NOT EXISTS ontology_constraints (
  constraint_id VARCHAR PRIMARY KEY,
  concept_id    VARCHAR NOT NULL,
  rule_type     VARCHAR NOT NULL,
  expression    VARCHAR NOT NULL,
  tolerance     DOUBLE,
  note          VARCHAR,
  source_uri    VARCHAR,
  severity      VARCHAR DEFAULT 'error'
);

-- 维度成员：把「分产品/分地区」的段名标准化，跨公司可归并（补丁 O1）。
CREATE TABLE IF NOT EXISTS dimension_members (
  dimension   VARCHAR NOT NULL,
  member_code VARCHAR NOT NULL,
  member_name VARCHAR NOT NULL,
  aliases     VARCHAR[],
  PRIMARY KEY (dimension, member_code)
);

CREATE INDEX IF NOT EXISTS idx_concepts_metric ON concepts (metric_id);
CREATE INDEX IF NOT EXISTS idx_concepts_parent ON concepts (parent_id);
CREATE INDEX IF NOT EXISTS idx_relations_src ON concept_relations (src_concept);
"""

# 补丁：severity 区分 error / warn。老库补列，幂等。
ONTOLOGY_MIGRATION_SQL = [
    "ALTER TABLE ontology_constraints ADD COLUMN IF NOT EXISTS severity VARCHAR DEFAULT 'error'",
]
