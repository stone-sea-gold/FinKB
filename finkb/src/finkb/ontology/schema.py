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

-- ======================= 公理层（ADR-057） =======================
-- 公理层与概念层同库：推理结果要和 facts / claims 同库 join，才能把
-- 「推断出什么」一路走回「哪条事实、哪一页」。
CREATE TABLE IF NOT EXISTS ontology_classes (
  class_id  VARCHAR PRIMARY KEY,
  label     VARCHAR NOT NULL,
  parent_id VARCHAR,
  comment   TEXT
);

CREATE TABLE IF NOT EXISTS ontology_object_properties (
  property_id     VARCHAR PRIMARY KEY,
  label           VARCHAR NOT NULL,
  domain_class    VARCHAR,
  range_class     VARCHAR,
  inverse_of      VARCHAR,
  characteristics VARCHAR[],
  comment         TEXT
);

CREATE TABLE IF NOT EXISTS ontology_property_chains (
  chain_id  VARCHAR PRIMARY KEY,
  properties VARCHAR[] NOT NULL,
  implies   VARCHAR NOT NULL,
  comment   TEXT
);

-- 数值定义类：OWL 2 RL 不支持算术，判定式由 infer 用确定性算子求值。
CREATE TABLE IF NOT EXISTS ontology_defined_classes (
  class_id         VARCHAR PRIMARY KEY,
  label            VARCHAR NOT NULL,
  parent_id        VARCHAR,
  expression       TEXT NOT NULL,
  definition       TEXT,
  severity         VARCHAR DEFAULT 'warn',
  evidence_metrics VARCHAR[],
  note             TEXT
);

-- 推理结果：每个 (公司,期间) 命中的定义类，带证据事实 id（血缘）。
CREATE TABLE IF NOT EXISTS inferred_concepts (
  company          VARCHAR NOT NULL,
  period           VARCHAR NOT NULL,
  class_id         VARCHAR NOT NULL,
  rule_id          VARCHAR NOT NULL,
  expression       TEXT,
  evidence_fact_ids BIGINT[],
  values_json      VARCHAR,
  severity         VARCHAR,
  run_id           VARCHAR,
  created_at       TIMESTAMP,
  PRIMARY KEY (company, period, class_id)
);

-- 属性链/逆属性/对称属性推出的边；premises_json 记录用了哪几条基础边。
CREATE TABLE IF NOT EXISTS inferred_edges (
  src_id      VARCHAR NOT NULL,
  rel_type    VARCHAR NOT NULL,
  dst_id      VARCHAR NOT NULL,
  rule_id     VARCHAR NOT NULL,
  premises_json VARCHAR,
  source_doc  VARCHAR,
  page_num    INTEGER,
  run_id      VARCHAR,
  created_at  TIMESTAMP,
  PRIMARY KEY (src_id, rel_type, dst_id, rule_id)
);

-- OWL RL 闭包里新长出来的三元组（相对 ABox 基线）。
CREATE TABLE IF NOT EXISTS inferred_triples (
  subject   VARCHAR NOT NULL,
  predicate VARCHAR NOT NULL,
  object    VARCHAR NOT NULL,
  rule_type VARCHAR,
  run_id    VARCHAR
);

CREATE TABLE IF NOT EXISTS inference_runs (
  run_id          VARCHAR PRIMARY KEY,
  started_at      TIMESTAMP,
  finished_at     TIMESTAMP,
  tbox_triples    BIGINT,
  abox_triples    BIGINT,
  inferred_triples BIGINT,
  defined_hits    BIGINT,
  chain_edges     BIGINT,
  status          VARCHAR,
  note            VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_concepts_metric ON concepts (metric_id);
CREATE INDEX IF NOT EXISTS idx_concepts_parent ON concepts (parent_id);
CREATE INDEX IF NOT EXISTS idx_relations_src ON concept_relations (src_concept);
CREATE INDEX IF NOT EXISTS idx_inferred_edges_src ON inferred_edges (src_id);
CREATE INDEX IF NOT EXISTS idx_inferred_concepts_class ON inferred_concepts (class_id);
"""

# 补丁：severity 区分 error / warn。老库补列，幂等。
ONTOLOGY_MIGRATION_SQL = [
    "ALTER TABLE ontology_constraints ADD COLUMN IF NOT EXISTS severity VARCHAR DEFAULT 'error'",
]
