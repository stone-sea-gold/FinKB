"""L1 事实层的 DuckDB 仓储。

只负责「建表 + 写 + 读」，不做抽取、不做单位换算——那些在
:mod:`finkb.fact_engine.extractor` / :mod:`finkb.fact_engine.verifier`（待实现）。

表结构与方案 v2.2 §7.2 一致；其中两处是方案的硬约束，不要为了省事改掉：

- ``facts`` 的唯一键含 ``source_doc``：同一 (公司, 期间, 指标, 口径) 在年报和
  半年报里各出现一次时都要保留，由 ``facts_dedup`` 按**公告日期**挑最新。
- ``unmatched_pool`` 必须落库：抽取失败的 Observation 不能静默丢，
  否则「exact 匹配率 ≥80%」永远无法解释剩下的 20% 去哪了。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import duckdb

from finkb.fact_engine.metric_dict import Metric

SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS fact_id_seq START 1;

CREATE TABLE IF NOT EXISTS meta (
  key   VARCHAR PRIMARY KEY,
  value VARCHAR
);

CREATE TABLE IF NOT EXISTS metric_dict (
  metric_id    VARCHAR PRIMARY KEY,
  metric_name  VARCHAR,
  unit_default VARCHAR,
  category     VARCHAR,
  aliases      VARCHAR[]
);

CREATE TABLE IF NOT EXISTS facts (
  fact_id           BIGINT PRIMARY KEY,
  company           VARCHAR,
  period            VARCHAR,
  metric_id         VARCHAR,
  unit_family       VARCHAR,
  currency          VARCHAR,
  unit_of_account   VARCHAR,
  norm_unit         VARCHAR,
  value_raw         DOUBLE,
  raw_text_matched  VARCHAR,
  value_norm        DOUBLE,
  caliber           VARCHAR,
  reporting_party   VARCHAR,
  report_date       DATE,
  source_doc        VARCHAR,
  page_num          INTEGER,
  table_id          VARCHAR,
  match_level       VARCHAR,
  extracted_at      TIMESTAMP,
  dict_version      INTEGER,
  period_basis      VARCHAR,
  UNIQUE (company, period, metric_id, caliber, source_doc)
);


CREATE TABLE IF NOT EXISTS relations (
  src_type   VARCHAR,
  src_id     VARCHAR,
  rel_type   VARCHAR,
  dst_type   VARCHAR,
  dst_id     VARCHAR,
  source_doc VARCHAR,
  page_num   INTEGER,
  attrs_json VARCHAR,
  period     VARCHAR,
  src_entity_id VARCHAR,
  dst_entity_id VARCHAR
);

-- 实体主数据（ADR-059）：关系边里的人/机构名先过门禁，再在这里消歧。
-- entity_id 是稳定主键（co:000002 / org:<hash> / person:<hash>），dst_id 仍保留原文，
-- 方便回查；跨文档同名实体靠别名表合并。
CREATE TABLE IF NOT EXISTS entities_master (
  entity_id      VARCHAR PRIMARY KEY,
  canonical_name VARCHAR,
  entity_type    VARCHAR,
  aliases_json   VARCHAR,
  source         VARCHAR
);

CREATE TABLE IF NOT EXISTS entity_aliases (
  alias      VARCHAR,
  entity_id  VARCHAR,
  alias_type VARCHAR
);

-- 附注锚点（S2）：指标 → 报表页（facts 来源）+ 附注章节页（section 标题匹配）。
CREATE TABLE IF NOT EXISTS note_anchors (
  doc_id      VARCHAR,
  company     VARCHAR,
  period      VARCHAR,
  metric_id   VARCHAR,
  anchor_type VARCHAR,
  page_num    INTEGER,
  section     VARCHAR,
  title       VARCHAR,
  source      VARCHAR
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_note_anchors
  ON note_anchors (doc_id, metric_id, anchor_type, page_num, section, period);

-- facts 级跨文档冲突（ADR-046）：同一 company+metric+period+caliber 在不同文档里数值不一致。
CREATE TABLE IF NOT EXISTS conflicts (
  conflict_id VARCHAR PRIMARY KEY,
  company     VARCHAR,
  metric_id   VARCHAR,
  period      VARCHAR,
  caliber     VARCHAR,
  fact_id_a   BIGINT,
  fact_id_b   BIGINT,
  value_a     DOUBLE,
  value_b     DOUBLE,
  doc_a       VARCHAR,
  doc_b       VARCHAR,
  detected_at VARCHAR
);

-- 分产品 / 分行业 / 分地区结构（ADR-045）。长表：一行一个（维度, 段名, 指标）。
CREATE SEQUENCE IF NOT EXISTS segment_id_seq START 1;

CREATE TABLE IF NOT EXISTS segment_facts (
  segment_id   BIGINT PRIMARY KEY,
  company      VARCHAR,
  period       VARCHAR,
  dimension    VARCHAR,
  segment_name VARCHAR,
  metric       VARCHAR,
  value_norm   DOUBLE,
  unit         VARCHAR,
  source_doc   VARCHAR,
  page_num     INTEGER,
  table_id     VARCHAR,
  member_id    VARCHAR,
  UNIQUE (company, period, dimension, segment_name, metric, source_doc)
);

-- 维度层（ADR-054）：维度种类是封闭词表，成员是数据驱动的开放集合。
-- 这样每份新年报都不需要专用维度配置：种类由准则/披露规则固定（product /
-- industry / region / channel / customer / currency），成员由结构表行名自动
-- 抽取，member_id 做归一（同一成员的多种写法合并到一条）。
CREATE TABLE IF NOT EXISTS dimensions (
  dim_kind VARCHAR PRIMARY KEY,   -- product / industry / region / ...
  label    VARCHAR,               -- 分产品 / 分行业 / 分地区
  markers  VARCHAR,               -- 识别用的行标记/表题关键词，竖线分隔
  note     VARCHAR
);

CREATE TABLE IF NOT EXISTS dimension_members (
  member_id  VARCHAR PRIMARY KEY,  -- product:新能源汽车
  dim_kind   VARCHAR,
  label      VARCHAR,              -- 归一后的成员名
  parent_id  VARCHAR,              -- 成员层级，如 region:境内 → region:华东
  aliases    VARCHAR,              -- 见过的其它写法，竖线分隔
  first_seen VARCHAR,              -- 首次出现的 source_doc，便于溯源
  facts      INTEGER               -- 该成员已落的 segment_facts 条数
);

CREATE TABLE IF NOT EXISTS unmatched_pool (
  id               BIGINT PRIMARY KEY,
  observation_json JSON,
  fail_reason      VARCHAR,
  created_at       TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS unmatched_id_seq START 1;

CREATE INDEX IF NOT EXISTS idx_facts_company_period ON facts (company, period);
"""

FACTS_DEDUP_VIEW_SQL = """CREATE OR REPLACE VIEW facts_dedup AS
SELECT * FROM (
  SELECT *, row_number() OVER (
    PARTITION BY company, period, metric_id, caliber
    ORDER BY report_date DESC, source_doc DESC
  ) AS rn FROM facts
) t WHERE rn = 1;
"""


class FactStore:
    """一个 DuckDB 文件（或内存库）上的 L1 仓储。"""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.conn = duckdb.connect(str(db_path))
        self.conn.execute(SCHEMA_SQL)
        # 旧库升级：relations 加 attrs_json（ADR-046）；facts 加 dict_version（补丁 O3）。
        self.conn.execute("ALTER TABLE relations ADD COLUMN IF NOT EXISTS attrs_json VARCHAR")
        # S2 实体治理：关系边加期间与实体主键（ADR-059）。
        self.conn.execute("ALTER TABLE relations ADD COLUMN IF NOT EXISTS period VARCHAR")
        self.conn.execute("ALTER TABLE relations ADD COLUMN IF NOT EXISTS src_entity_id VARCHAR")
        self.conn.execute("ALTER TABLE relations ADD COLUMN IF NOT EXISTS dst_entity_id VARCHAR")
        # 旧库的 note_anchors 唯一索引不含 period，重建成含 period 的版本。
        self.conn.execute("DROP INDEX IF EXISTS uq_note_anchors")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_note_anchors "
            "ON note_anchors (doc_id, metric_id, anchor_type, page_num, section, period)"
        )
        self.conn.execute("ALTER TABLE facts ADD COLUMN IF NOT EXISTS dict_version INTEGER")
        # 期间口径（ADR-055）：ROE 这类率值在半年/季报里是年化值，不标出来就会
        # 和年报的年度值混在一起排队。
        self.conn.execute("ALTER TABLE facts ADD COLUMN IF NOT EXISTS period_basis VARCHAR")
        # 视图在补列之后建：SELECT * 的列在创建时绑定，必须晚于上面的 ALTER。
        self.conn.execute(FACTS_DEDUP_VIEW_SQL)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "FactStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 指标字典 ---------------------------------------------------------

    def load_metric_dict(self, metrics: Iterable[Metric]) -> int:
        """把字典 upsert 进库，返回行数。

        用 ``INSERT OR REPLACE`` 而不是清表重建：字典是事实表的外键来源，
        重建会让已有的 facts 指向一个刚消失又刚出现的 ID，审计时看不出区别。
        """
        rows = [
            (m.metric_id, m.metric_name, m.unit_default, m.category, list(m.aliases))
            for m in metrics
        ]
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT OR REPLACE INTO metric_dict VALUES (?, ?, ?, ?, ?)", rows
        )
        return len(rows)

    # ---- 元信息 -----------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        """写入一条库级元信息（字典版本、模型、构建时间等）。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value]
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return row[0] if row else None

    def all_meta(self) -> dict[str, str]:
        return dict(self.conn.execute("SELECT key, value FROM meta").fetchall())

    def metrics(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT metric_id, metric_name, unit_default, category, aliases "
            "FROM metric_dict ORDER BY metric_id"
        ).fetchall()

    # ---- 事实 -------------------------------------------------------------

    def insert_fact(
        self,
        *,
        company: str,
        period: str,
        metric_id: str,
        unit_family: str,
        unit_of_account: str,
        norm_unit: str,
        value_raw: float | None,
        raw_text_matched: str,
        value_norm: float | None,
        caliber: str,
        reporting_party: str,
        report_date: date | str,
        source_doc: str,
        page_num: int,
        table_id: str,
        match_level: str,
        currency: str = "CNY",
        extracted_at: datetime | None = None,
        dict_version: int | None = None,
        period_basis: str | None = None,
    ) -> int:
        """写一条事实，返回 ``fact_id``。重复的 (公司,期间,指标,口径,文档) 会抛错。"""
        fact_id = self.conn.execute("SELECT nextval('fact_id_seq')").fetchone()[0]
        self.conn.execute(
            "INSERT INTO facts (fact_id, company, period, metric_id, unit_family, currency, "
            "unit_of_account, norm_unit, value_raw, raw_text_matched, value_norm, caliber, "
            "reporting_party, report_date, source_doc, page_num, table_id, match_level, "
            "extracted_at, dict_version, period_basis) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                fact_id,
                company,
                period,
                metric_id,
                unit_family,
                currency,
                unit_of_account,
                norm_unit,
                value_raw,
                raw_text_matched,
                value_norm,
                caliber,
                reporting_party,
                report_date,
                source_doc,
                page_num,
                table_id,
                match_level,
                extracted_at or datetime.now(),
                dict_version,
                period_basis,
            ],
        )
        return fact_id

    def facts(self, where: str = "", params: list | None = None) -> list[tuple]:
        sql = "SELECT * FROM facts"
        if where:
            sql += f" WHERE {where}"
        return self.conn.execute(sql, params or []).fetchall()

    def dedup_facts(self) -> list[tuple]:
        return self.conn.execute("SELECT * FROM facts_dedup").fetchall()

    def get_fact(self, fact_id: int) -> dict | None:
        """按 id 取一行事实（返回列名字典，L2 的 claim 校验用）。"""
        row = self.conn.execute("SELECT * FROM facts WHERE fact_id = ?", [fact_id]).fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in self.conn.description], row))

    # ---- 维度层（ADR-055） ------------------------------------------------

    def upsert_dimension(self, *, dim_kind: str, label: str, markers: str, note: str = "") -> None:
        """登记一个维度种类。封闭词表，来自本体；重灌幂等。"""
        self.conn.execute(
            "INSERT INTO dimensions VALUES (?, ?, ?, ?) "
            "ON CONFLICT (dim_kind) DO UPDATE SET label = EXCLUDED.label, "
            "markers = EXCLUDED.markers, note = EXCLUDED.note",
            [dim_kind, label, markers, note],
        )

    def upsert_segment_member(self, *, dim_kind: str, raw_label: str, source_doc: str) -> str:
        """把结构表的一行段名归一成一个维度成员，返回 ``member_id``。

        归一规则：去空白/全角空格/中英文括号与冒号，再剥「其中」前缀。这样
        「其中：新能源汽车」与「新能源汽车」会落到同一个成员上；其余不同写法
        （如「汽车」vs「汽车行业」）**不**归并，而是记为不同成员、原文进 ``aliases``——
        归并是数据治理决策，不该由归一化函数擅自拍板。
        """
        norm = self._norm_member(raw_label)
        member_id = f"{dim_kind}:{norm}"
        exists = self.conn.execute(
            "SELECT aliases, facts FROM dimension_members WHERE member_id = ?", [member_id]
        ).fetchone()
        raw = str(raw_label).strip()
        if exists is None:
            self.conn.execute(
                "INSERT INTO dimension_members VALUES (?, ?, ?, ?, ?, ?, ?)",
                [member_id, dim_kind, norm, None, raw, source_doc, 1],
            )
        else:
            known = [a for a in str(exists[0] or "").split("|") if a]
            if raw and raw not in known:
                known.append(raw)
            # 去重保序：同一成员被同一写法反复命中时，aliases 不该无限变长。
            merged = list(dict.fromkeys([norm, *known]))
            self.conn.execute(
                "UPDATE dimension_members SET facts = ?, aliases = ? WHERE member_id = ?",
                [int(exists[1] or 0) + 1, "|".join(merged), member_id],
            )
        return member_id

    @staticmethod
    def _norm_member(text: str) -> str:
        """成员名归一：去空白/括号/冒号，并去掉「其中」前缀。

        「其中：新能源汽车」与「新能源汽车」是同一成员的不同印法——前者是报表里
        的拆分子项，后者是主项。归一后落到同一个 member_id，别名留原文。
        """
        import re as _re

        norm = _re.sub(r"[\s\u3000（）()：:，,]", "", str(text or ""))
        return _re.sub(r"^其中", "", norm)

    def dimensions(self) -> list[tuple]:
        return self.conn.execute("SELECT * FROM dimensions ORDER BY dim_kind").fetchall()

    def dimension_members(self, dim_kind: str | None = None) -> list[tuple]:
        sql = "SELECT * FROM dimension_members"
        args: list[str] = []
        if dim_kind:
            sql += " WHERE dim_kind = ?"
            args.append(dim_kind)
        return self.conn.execute(sql + " ORDER BY dim_kind, member_id", args).fetchall()

    # ---- 结构（分产品 / 分行业 / 分地区） ---------------------------------

    def insert_segment_fact(
        self,
        *,
        company: str,
        period: str,
        dimension: str,
        segment_name: str,
        metric: str,
        value_norm: float,
        unit: str,
        source_doc: str,
        page_num: int,
        table_id: str,
        member_id: str | None = None,
    ) -> int:
        """写一条结构事实；同键重复时覆盖（重灌幂等）。"""
        segment_id = self.conn.execute("SELECT nextval('segment_id_seq')").fetchone()[0]
        self.conn.execute(
            "INSERT INTO segment_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (company, period, dimension, segment_name, metric, source_doc) "
            "DO UPDATE SET value_norm = EXCLUDED.value_norm, unit = EXCLUDED.unit, "
            "page_num = EXCLUDED.page_num, table_id = EXCLUDED.table_id, "
            "member_id = EXCLUDED.member_id",
            [
                segment_id,
                company,
                period,
                dimension,
                segment_name,
                metric,
                value_norm,
                unit,
                source_doc,
                page_num,
                table_id,
                member_id,
            ],
        )
        return segment_id

    def segment_facts(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT * FROM segment_facts "
            "ORDER BY company, period, dimension, segment_name, metric"
        ).fetchall()

    # ---- 未匹配池 ---------------------------------------------------------

    def add_unmatched(self, observation: dict, fail_reason: str) -> int:
        """记一条没抽成的 Observation；``fail_reason`` 见方案 §7.2。"""
        if fail_reason not in {"literal_not_found", "metric_not_in_dict", "schema_invalid"}:
            raise ValueError(f"未知 fail_reason: {fail_reason}")
        uid = self.conn.execute("SELECT nextval('unmatched_id_seq')").fetchone()[0]
        self.conn.execute(
            "INSERT INTO unmatched_pool VALUES (?, ?, ?, ?)",
            [uid, observation, fail_reason, datetime.now()],
        )
        return uid

    def unmatched(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT * FROM unmatched_pool ORDER BY id"
        ).fetchall()
