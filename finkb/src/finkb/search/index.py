"""L2 主检索：SQLite FTS5 建索引、bm25 排序与中文别名扩展（ADR-002）。

为什么不用 jieba：环境里没装，也不许加新依赖。sqlite3 3.45 自带 trigram
tokenizer，把中文按「3 字滑窗」建索引，不需要分词词典。代价是**查询词至少
3 个字才可能命中**，所以「营收」这类 2 字口语词要靠 ``metric_dict.yaml`` 的
别名扩展成全称（「营收」→「营业收入」）才会召回。

索引库是派生数据（``--rebuild`` 全量重灌，随时可重建），不进 git。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from finkb.wiki.vault import DEFAULT_VAULT, iter_pages

FINKB_ROOT = Path(__file__).resolve().parents[3]
# 索引库放 data/_search/：沿用 .gitignore 里 `finkb/data/_*/` 的派生数据目录约定，
# 不用为了一个可重建的索引去改 .gitignore。
DEFAULT_DB = FINKB_ROOT / "data" / "_search" / "search.db"
DEFAULT_METRIC_DICT = FINKB_ROOT / "config" / "metric_dict.yaml"

# bm25 的列权重，顺序同 pages_fts 的列（path, title, description, body）：
# 标题命中比正文命中更相关，给 10 倍；path 只给 1 倍（文件名也算线索，但不该压过正文）。
BM25_WEIGHTS = "1.0, 10.0, 1.0, 1.0"
# snippet 第 2 个参数 -1 = 自动选最相关的那一列；24 个 token ≈ 70 字上下文。
SNIPPET = "snippet(pages_fts, -1, '<<', '>>', '…', 24)"

# pages 存元数据与正文（过滤用），pages_fts 只负责全文检索；两边用 rowid 对齐。
SCHEMA = """
CREATE TABLE pages (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    period TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT ''
);
CREATE VIRTUAL TABLE pages_fts USING fts5(path, title, description, body, tokenize='trigram');
"""

FILTER_FIELDS = ("type", "company", "period", "status")
TAG_FIELDS = ("tag", "tags")


def build_index(vault_root=DEFAULT_VAULT, db_path=DEFAULT_DB) -> int:
    """全量重建索引（先删表再灌，天然幂等）；返回索引的页面数。

    active 与 ``_draft/`` 的页面都进索引：draft 只是排在后面，不丢。
    """
    vault_root = Path(vault_root)
    db_path = Path(db_path)
    pages = iter_pages(vault_root, include_draft=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("DROP TABLE IF EXISTS pages; DROP TABLE IF EXISTS pages_fts;" + SCHEMA)
        for page_id, page in enumerate(pages, start=1):
            meta = page.meta
            title = str(meta.get("title") or "")
            description = str(meta.get("description") or "")
            rel = page.path.relative_to(vault_root).as_posix()
            conn.execute(
                "INSERT INTO pages (id, path, title, description, body, type, company, "
                "period, tags, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    page_id,
                    rel,
                    title,
                    description,
                    page.body,
                    str(meta.get("type") or ""),
                    str(meta.get("company") or ""),
                    str(meta.get("period") or ""),
                    json.dumps(meta.get("tags") or [], ensure_ascii=False),
                    str(meta.get("status") or ""),
                ),
            )
            conn.execute(
                "INSERT INTO pages_fts (rowid, path, title, description, body) "
                "VALUES (?, ?, ?, ?, ?)",
                (page_id, rel, title, description, page.body),
            )
        conn.commit()
    finally:
        conn.close()
    return len(pages)


# ---- 别名扩展（L2 主检索的「中文同义词」来源） --------------------------------


def load_metrics(path=DEFAULT_METRIC_DICT) -> list[dict]:
    """读指标字典里的 metrics 列表。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return list(data.get("metrics") or [])


def _normalize(term: object) -> str:
    """去空白 + 小写，用于别名匹配（「营 收」与「营收」算同一个词）。"""
    return "".join(str(term).split()).lower()


def expand_query(q, metric_dict_path=DEFAULT_METRIC_DICT) -> list[str]:
    """按指标字典把查询词扩展成同义词组（含原词，去重保序）。

    命中规则（任一）：指标的 id / 全称 / 别名出现在查询里，或查询是它们的
    子串。后者让 2 字口语词也能落到全称上（「净利」→「归母净利润」），
    因为 trigram 索引查不了 2 个字。
    """
    original = str(q).strip()
    term = _normalize(original)
    if not term:
        return []
    terms = [original]
    if len(term) < 2:
        return terms  # 单字太宽，不扩展
    seen = {term}
    for metric in load_metrics(metric_dict_path):
        fields = [metric.get("metric_id"), metric.get("metric_name")]
        fields += list(metric.get("aliases") or [])
        keys = {_normalize(f) for f in fields if f}
        if not any(k in term or term in k for k in keys if k):
            continue
        names = [metric.get("metric_name")] + list(metric.get("aliases") or [])
        for name in names:
            key = _normalize(name)
            if key and key not in seen:
                seen.add(key)
                terms.append(str(name))
    return terms


def _quote(term: str) -> str:
    """FTS5 查询串必须整体加引号，否则 8,039.65 里的逗号会被当成语法。"""
    return '"' + term.replace('"', '""') + '"'


def _match_expr(q: str, metric_dict_path=DEFAULT_METRIC_DICT) -> str:
    """拼 FTS5 MATCH 表达式：空白分词，词内别名 OR、词间 AND。"""
    groups: list[str] = []
    for token in q.split():
        # trigram 最少要 3 个字，2 字词（原词）在这里被丢掉，留下的别名全称仍可命中
        terms = [t for t in expand_query(token, metric_dict_path) if len(_normalize(t)) >= 3]
        if terms:
            groups.append("(" + " OR ".join(_quote(t) for t in terms) + ")")
    return " AND ".join(groups)


# ---- 检索 --------------------------------------------------------------------


def search(
    q,
    limit: int = 10,
    filters: dict | None = None,
    *,
    db_path=DEFAULT_DB,
    metric_dict_path=DEFAULT_METRIC_DICT,
) -> list[tuple[str, str, str, float]]:
    """检索知识页，返回 ``[(path, title, snippet, score)]``。

    - ``path`` 相对 vault 根目录（如 ``summaries/doc_a.md``）；
    - ``score`` 是 FTS5 bm25 原值，**越小越相关**；标题列权重 10；
    - draft 页不丢弃，但整档排在 active 之后；``filters={"status": "draft"}`` 可单查；
    - ``filters`` 支持 type / company / period / status / tag / tags。
    """
    q = str(q or "").strip()
    if not q or limit <= 0:
        return []
    match = _match_expr(q, metric_dict_path)
    if not match:
        return []
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"索引库不存在：{db_path}（先跑 python -m finkb.search --rebuild）"
        )

    conditions = ["pages_fts MATCH ?"]
    params: list[object] = [match]
    for key, value in (filters or {}).items():
        if key in FILTER_FIELDS:
            # 字段名来自白名单，取值走参数，不拼用户输入
            conditions.append(f"p.{key} = ?")
            params.append(str(value))
        elif key in TAG_FIELDS:
            for tag in value if isinstance(value, (list, tuple)) else [value]:
                conditions.append(
                    "EXISTS (SELECT 1 FROM json_each(p.tags) WHERE json_each.value = ?)"
                )
                params.append(str(tag))
        else:
            raise ValueError(
                f"不支持的过滤字段：{key}（可用 {'/'.join(FILTER_FIELDS)}/tag/tags）"
            )
    params.append(int(limit))
    sql = (
        f"SELECT p.path, p.title, {SNIPPET}, bm25(pages_fts, {BM25_WEIGHTS}) "
        "FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid "
        f"WHERE {' AND '.join(conditions)} "
        f"ORDER BY (p.status = 'draft') ASC, bm25(pages_fts, {BM25_WEIGHTS}) ASC "
        "LIMIT ?"
    )
    conn = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()
