"""实体页：把 relations 表里的关系边汇成公司实体页（确定性生成，不用 LLM）。

页面只写「关系 + 页锚」，不写数值：关系边的出处是结构化表（主要子公司、
关联方、前十大股东、重大担保），每个条目都带 ``[Ref: 文档#P页码]``。
"""

from __future__ import annotations

from pathlib import Path

from finkb.fact_engine.selector import FINKB_ROOT, load_manifest
from finkb.fact_engine.store import FactStore
from finkb.wiki import frontmatter
from finkb.wiki.vault import rebuild_index, write_page

DEFAULT_VAULT = FINKB_ROOT / "wiki_vault"

# rel_type → (小节标题, 排序)
REL_SECTIONS = (
    ("subsidiary_of", "子公司"),
    ("shareholder_of", "主要股东"),
    ("related_party_of", "关联方"),
    ("guarantees_for", "对外担保对象"),
)

COMPANY_NAMES = load_manifest()


def _name(code: str, year: str = "", kind: str = "") -> str:
    """从 manifest 取公司名；取不到就退回代码。"""
    for key in ((code, year, kind),):
        row = COMPANY_NAMES.get(key) or {}
        if row.get("公司名称"):
            return row["公司名称"]
    for (c, _y, _k), row in COMPANY_NAMES.items():
        if c == code and row.get("公司名称"):
            return row["公司名称"]
    return code


def _edges(store: FactStore, code: str) -> list[dict]:
    rows = store.conn.execute(
        "SELECT rel_type, dst_id, source_doc, page_num FROM relations "
        "WHERE src_id = ? AND rel_type IN ('subsidiary_of','shareholder_of','related_party_of','guarantees_for') "
        "ORDER BY rel_type, dst_id",
        [code],
    ).fetchall()
    edges = []
    for rel_type, target, source_doc, page in rows:
        target = str(target or "").strip()
        # 担保表里的「实际担保总额(即A4+B4+C4)…」这类公式行不是主体，跳过。
        if not target or "(" in target or "（" in target:
            continue
        if any(word in target for word in ("占公司净资产", "占净资产", "合计", "小计")):
            continue
        edges.append({"rel_type": rel_type, "target": target, "source_doc": source_doc, "page": page})
    return edges


def build_entity_body(company_name: str, code: str, edges: list[dict]) -> str:
    lines = [f"# {company_name}（{code}）", ""]
    for rel_type, title in REL_SECTIONS:
        items = [e for e in edges if e["rel_type"] == rel_type]
        if not items:
            continue
        lines.append(f"## {title}")
        for edge in items:
            anchor = f"[Ref: {edge['source_doc']}#P{edge['page']}]"
            lines.append(f"- {edge['target']} {anchor}")
        lines.append("")
    if len(lines) == 2:
        lines.append("本公司在已解析年报里没有可提取的关系表。")
        lines.append("")
    lines.append("## 数据来源")
    docs = sorted({e["source_doc"] for e in edges})
    for doc in docs:
        lines.append(f"- {doc}[Ref: {doc}#P1]")
    lines.append("")
    return "\n".join(lines)


def compile_entities(store: FactStore, *, vault_root: Path = DEFAULT_VAULT) -> list[str]:
    """为 facts 里出现过的每家公司生成/更新一张实体页，返回页面 slug 列表。"""
    codes = [
        r[0]
        for r in store.conn.execute(
            "SELECT company, min(source_doc) FROM facts GROUP BY company ORDER BY company"
        ).fetchall()
    ]
    written: list[str] = []
    for code in codes:
        first_doc = store.conn.execute(
            "SELECT source_doc, report_date FROM facts WHERE company = ? LIMIT 1", [code]
        ).fetchone()
        doc, _report_date = (first_doc or ("", ""))
        year = doc.split("_")[1] if doc else ""
        kind = doc.split("_")[2] if doc else ""
        name = _name(code, year, kind)
        edges = _edges(store, code)
        body = build_entity_body(name, code, edges)
        fact_row = store.conn.execute(
            "SELECT fact_id FROM facts WHERE company = ? ORDER BY fact_id LIMIT 1", [code]
        ).fetchone()
        sources = [f"l0:{doc}"] if doc else []
        if fact_row:
            sources.append(f"l1:facts#{fact_row[0]}")
        meta = frontmatter.build(
            page_type="entity",
            title=f"{name}（{code}）",
            description=f"{name}的子公司、股东与关联方关系（来自年报结构化表）",
            sources=sources,
            company=code,
            period=year,
            tags=["实体", "关系"],
            status="active",
        )
        write_page(vault_root, "entity", f"{code}-{name}", meta, body, status="active")
        written.append(f"{code}-{name}")
    rebuild_index(vault_root)
    return written


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI 入口
    import argparse

    parser = argparse.ArgumentParser(prog="python -m finkb.wiki.entities")
    parser.add_argument("--db", default=str(FINKB_ROOT / "data" / "facts.duckdb"))
    parser.add_argument("--vault", default=str(DEFAULT_VAULT))
    args = parser.parse_args(argv)
    with FactStore(Path(args.db)) as store:
        pages = compile_entities(store, vault_root=Path(args.vault))
    print(f"已生成 {len(pages)} 张实体页")
    for slug in pages:
        print(" ", slug)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
