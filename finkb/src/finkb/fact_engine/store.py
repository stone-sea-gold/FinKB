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
  UNIQUE (company, period, metric_id, caliber, source_doc)
);

CREATE OR REPLACE VIEW facts_dedup AS
SELECT * FROM (
  SELECT *, row_number() OVER (
    PARTITION BY company, period, metric_id, caliber
    ORDER BY report_date DESC, source_doc DESC
  ) AS rn FROM facts
) t WHERE rn = 1;

CREATE TABLE IF NOT EXISTS relations (
  src_type   VARCHAR,
  src_id     VARCHAR,
  rel_type   VARCHAR,
  dst_type   VARCHAR,
  dst_id     VARCHAR,
  source_doc VARCHAR,
  page_num   INTEGER
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


class FactStore:
    """一个 DuckDB 文件（或内存库）上的 L1 仓储。"""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.conn = duckdb.connect(str(db_path))
        self.conn.execute(SCHEMA_SQL)

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
    ) -> int:
        """写一条事实，返回 ``fact_id``。重复的 (公司,期间,指标,口径,文档) 会抛错。"""
        fact_id = self.conn.execute("SELECT nextval('fact_id_seq')").fetchone()[0]
        self.conn.execute(
            "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
