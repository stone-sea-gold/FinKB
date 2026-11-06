"""页面数字的 claim 账本与渲染（ADR-044）。

核心不变量「数字只由 SQL 计算」在 L2 的落地方式：

- 页面正文只写占位符 ``{{cN}}``；模型看不到也写不出真实数字；
- 渲染时由代码把 ``cN`` 解析成 fact / query / quote 三类 claim 之一，
  写入 ``data/wiki.duckdb`` 的 ``claims`` 表，并在正文里换成
  「数字 + 脚注标记」，脚注定义附在页尾；
- lint 规则 9 再回头核对「正文里每个数字都被同句的 claim 覆盖」。

``claims`` 表是唯一真相：脚注文本、显示值都由它生成，页面本身不保存副本。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml

from finkb.common import paths

DEFAULT_CLAIMS_DB = paths.wiki_db()
DEFAULT_QUERIES = paths.config_dir() / "wiki_queries.yaml"

CLAIM_KINDS = ("fact", "query", "quote")
PLACEHOLDER = re.compile(r"\{\{(c\d+)\}\}")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS claims (
    claim_id VARCHAR PRIMARY KEY,
    page VARCHAR NOT NULL,
    ordinal INTEGER NOT NULL,
    kind VARCHAR NOT NULL,
    fact_id BIGINT,
    segment_id BIGINT,
    query_id VARCHAR,
    params VARCHAR,
    anchor VARCHAR,
    display_value VARCHAR NOT NULL,
    raw_value DOUBLE,
    unit VARCHAR,
    note VARCHAR,
    created_at VARCHAR NOT NULL,
    UNIQUE (page, ordinal)
);
"""


@dataclass
class Claim:
    page: str
    ordinal: int
    kind: str
    display_value: str
    raw_value: float | None = None
    unit: str = ""
    fact_id: int | None = None
    segment_id: int | None = None
    query_id: str | None = None
    params: dict = field(default_factory=dict)
    anchor: str | None = None
    note: str = ""

    @property
    def marker(self) -> str:
        return f"c{self.ordinal}"


def format_value(value: float, unit: str) -> str:
    """按单位决定小数位：亿元两位、百分比两位、每股四位。"""
    if unit == "%":
        return f"{value:.2f}%"
    if unit == "元/股":
        return f"{value:.4f}"
    return f"{value:,.2f}"


class ClaimStore:
    """``data/wiki.duckdb`` 的连接封装。"""

    def __init__(self, path: Path | str = DEFAULT_CLAIMS_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.path))
        self.conn.execute(SCHEMA_SQL)
        self.conn.execute("ALTER TABLE claims ADD COLUMN IF NOT EXISTS segment_id BIGINT")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ClaimStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def delete_page(self, page: str) -> None:
        self.conn.execute("DELETE FROM claims WHERE page = ?", [page])

    def insert_many(self, claims: list[Claim]) -> None:
        if not claims:
            return
        now = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
        for claim in claims:
            self.conn.execute(
                "INSERT INTO claims (claim_id, page, ordinal, kind, fact_id, segment_id, query_id, "
                "params, anchor, display_value, raw_value, unit, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    f"{claim.page}#c{claim.ordinal}",
                    claim.page,
                    claim.ordinal,
                    claim.kind,
                    claim.fact_id,
                    claim.segment_id,
                    claim.query_id,
                    json.dumps(claim.params, ensure_ascii=False, sort_keys=True) if claim.params else None,
                    claim.anchor,
                    claim.display_value,
                    claim.raw_value,
                    claim.unit,
                    claim.note,
                    now,
                ],
            )

    def for_page(self, page: str) -> list[Claim]:
        rows = self.conn.execute(
            "SELECT page, ordinal, kind, fact_id, segment_id, query_id, params, anchor, display_value, raw_value, unit, note "
            "FROM claims WHERE page = ? ORDER BY ordinal",
            [page],
        ).fetchall()
        out: list[Claim] = []
        for row in rows:
            out.append(
                Claim(
                    page=row[0],
                    ordinal=row[1],
                    kind=row[2],
                    fact_id=row[3],
                    segment_id=row[4],
                    query_id=row[5],
                    params=json.loads(row[6]) if row[6] else {},
                    anchor=row[7],
                    display_value=row[8],
                    raw_value=row[9],
                    unit=row[10] or "",
                    note=row[11] or "",
                )
            )
        return out

    def count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM claims").fetchone()[0]


# ---- 三类 claim 的解析 ------------------------------------------------------


def claim_from_fact(page: str, ordinal: int, fact: dict) -> Claim:
    """fact 类：显示值 = facts 行按 norm_unit 格式化的结果。"""
    value = float(fact["value_norm"])
    unit = str(fact.get("norm_unit") or "")
    return Claim(
        page=page,
        ordinal=ordinal,
        kind="fact",
        fact_id=int(fact["fact_id"]),
        raw_value=value,
        unit=unit,
        display_value=format_value(value, unit),
        note=f"{fact.get('company')} {fact.get('period')} {fact.get('metric_id')}",
    )


def claim_from_segment(page: str, ordinal: int, row: dict) -> Claim:
    """segment 类：显示值 = segment_facts 行的数值（已折算成亿元/百分数）。"""
    value = float(row["value_norm"])
    unit = str(row.get("unit") or "")
    return Claim(
        page=page,
        ordinal=ordinal,
        kind="segment",
        segment_id=int(row["segment_id"]),
        raw_value=value,
        unit=unit,
        display_value=format_value(value, unit),
        note=f"{row.get('dimension')} {row.get('segment_name')} {row.get('metric')}",
    )


def claim_from_quote(page: str, ordinal: int, anchor: str, text: str, number: str) -> Claim:
    """quote 类：原文某页里能字面找到的数字。"""
    return Claim(
        page=page,
        ordinal=ordinal,
        kind="quote",
        anchor=anchor,
        display_value=number,
        note=text.strip()[:80],
    )


def load_queries(path: Path | str = DEFAULT_QUERIES) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("queries") or {}


def claim_from_query(
    page: str,
    ordinal: int,
    query_id: str,
    params: dict,
    *,
    facts_db: duckdb.DuckDBPyConnection,
    queries: dict | None = None,
) -> Claim:
    """query 类：跑命名 SQL，拿结果作为显示值。"""
    queries = queries if queries is not None else load_queries()
    spec = queries.get(query_id)
    if spec is None:
        raise KeyError(f"未定义的查询: {query_id}")
    order = list(spec.get("params") or [])
    values = [params.get(name) for name in order]
    row = facts_db.execute(spec["sql"], values).fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"查询 {query_id} 没有结果（参数 {params}）")
    value = float(row[0])
    unit = str(spec.get("unit") or "")
    return Claim(
        page=page,
        ordinal=ordinal,
        kind="query",
        query_id=query_id,
        params=dict(params),
        raw_value=value,
        unit=unit,
        display_value=format_value(value, unit),
        note=str(spec.get("description") or ""),
    )


# ---- 渲染 -------------------------------------------------------------------


def footnote(claim: Claim) -> str:
    """脚注定义文本（代码生成，模型不参与）。"""
    if claim.kind == "fact":
        return f"数值来源：facts #{claim.fact_id}（{claim.note}，{claim.unit}）。"
    if claim.kind == "segment":
        suffix = f"，{claim.unit}" if claim.unit else ""
        return f"结构数据来源：segment_facts #{claim.segment_id}（{claim.note}{suffix}）。"
    if claim.kind == "query":
        return f"计算来源：query `{claim.query_id}`（参数 {json.dumps(claim.params, ensure_ascii=False, sort_keys=True)}）。"
    return f"原文：{claim.anchor}。"


def render_body(template: str, claims: list[Claim]) -> str:
    """把 ``{{cN}}`` 换成「数字[^cN]」，并在末尾附脚注定义。"""
    by_marker = {claim.marker: claim for claim in claims}
    missing = {m for m in PLACEHOLDER.findall(template) if m not in by_marker}
    if missing:
        raise ValueError(f"正文占位符没有对应 claim: {sorted(missing)}")
    unused = set(by_marker) - set(PLACEHOLDER.findall(template))
    if unused:
        raise ValueError(f"claim 没有在正文使用: {sorted(unused)}")

    def replace(match: re.Match[str]) -> str:
        marker = match.group(1)
        claim = by_marker[marker]
        return f"{claim.display_value}[^{marker}]"

    body = PLACEHOLDER.sub(replace, template)
    notes = "\n".join(f"[^{claim.marker}]: {footnote(claim)}" for claim in claims)
    return body.rstrip("\n") + "\n\n" + notes + "\n"
