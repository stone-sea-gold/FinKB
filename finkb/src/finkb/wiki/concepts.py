"""概念页：跨文档对比（数字来自 L1 facts 的 claim 账本，ADR-044）。

与单文档编译器共用同一套「占位符 + claims + lint 门禁」，区别是 claim 目录
来自多份文档的同一指标，正文做横向对比。
"""

from __future__ import annotations

import re
from pathlib import Path

from finkb.fact_engine.extractor import LLMClient, flow_period
from finkb.fact_engine.selector import FINKB_ROOT, load_manifest
from finkb.fact_engine.store import FactStore
from finkb.lint import rules as lint_rules
from finkb.wiki import frontmatter
from finkb.wiki.claims import (
    PLACEHOLDER,
    Claim,
    ClaimStore,
    claim_from_fact,
    load_queries,
    render_body,
)
from finkb.wiki.compiler import DEFAULT_DATA_ROOT, DEFAULT_VAULT, STOCK_METRICS, SYSTEM_PROMPT, CatalogItem
from finkb.wiki.vault import page_path, read_page, write_page

CONCEPT_PROMPT = SYSTEM_PROMPT.replace(
    "你是财务知识库的编辑。你只能使用用户给出的「claim 目录」里的占位符来写数字。",
    "你是财务知识库的编辑，正在写一张跨公司对比的概念页。你只能使用用户给出的「claim 目录」里的占位符来写数字。",
) + """
9. 每个段落（包括开头段）至少带一条页锚 [Ref: 文档#P页码]，页码只能取自上面「每家公司可用的页锚」列表；
   涉及某家公司的事实性陈述，页锚只能引用该公司自己的文档。
10. 不要写「11 家」「3 家」这类数量词，也不要写任何算出来的数字（差值、倍数、占比）；
    比较差距用文字描述（如「显著高于」「略低」），金额只用目录里的占位符。
11. 对比时按数值从大到小组织叙述；正文里的公司名必须逐字使用目录里的公司名。"""


def _facts_by_doc(store: FactStore, doc_id: str, metric: str, period: str) -> dict | None:
    rows = store.conn.execute(
        "SELECT * FROM facts WHERE source_doc = ? AND metric_id = ?", [doc_id, metric]
    ).fetchall()
    columns = [d[0] for d in store.conn.execute("SELECT * FROM facts WHERE 1=0").description]
    records = [dict(zip(columns, r)) for r in rows]
    chosen = {str(r.get("period")): r for r in records}
    flow = flow_period(period)
    order = (period,) if flow == period else ((period,) if metric in STOCK_METRICS else (flow, period))
    for p in order:
        if p in chosen and chosen[p].get("value_norm") is not None:
            return chosen[p]
    return None


def _available_anchors(data_root: Path, doc_id: str, limit: int = 8) -> list[str]:
    path = data_root / "parsed" / doc_id / "page_anchored.md"
    if not path.exists():
        return [f"{doc_id}#P1"]
    pages = re.findall(r'"page":\s*(\d+)', path.read_text(encoding="utf-8")[:200000])
    ordered = list(dict.fromkeys(pages))
    return [f"{doc_id}#P{p}" for p in ordered[:limit]] or [f"{doc_id}#P1"]


def compile_concept(
    client: LLMClient,
    store: FactStore,
    *,
    slug: str,
    title: str,
    description: str,
    metric: str,
    period: str,
    doc_ids: list[str],
    vault_root: Path = DEFAULT_VAULT,
    claims_db: Path | None = None,
    data_root: Path = DEFAULT_DATA_ROOT,
    dry_run: bool = False,
) -> dict:
    """生成一张跨文档概念页；返回 {slug,status,...}。"""
    manifest = load_manifest()
    items: list[CatalogItem] = []
    anchors: list[str] = []
    facts_used: list[dict] = []
    for doc in doc_ids:
        fact = _facts_by_doc(store, doc, metric, period)
        if fact is None:
            continue
        code, year, kind = doc.split("_", 2)
        name = (manifest.get((code, year, kind)) or {}).get("公司名称") or code
        ordinal = len(items) + 1
        claim = claim_from_fact("", ordinal, fact)
        items.append(
            CatalogItem(
                marker=claim.marker,
                kind="fact",
                text=f"{name}（{doc}）{metric}",
                display=claim.display_value,
                unit=claim.unit,
                fact_id=claim.fact_id,
            )
        )
        facts_used.append({"doc": doc, "name": name})
        anchors.extend(_available_anchors(data_root, doc))
    if len(items) < 2:
        return {"slug": slug, "status": "skipped", "reason": "可对比的 claim 不足两条"}

    names = "、".join(f["name"] for f in facts_used)
    user = (
        f"概念：{title}\n期间：{period}\n对比公司：{names}\n\n"
        f"claim 目录（正文数字只能用这些占位符）：\n"
        + "\n".join(item.line() for item in items)
        + "\n\n每家公司可用的页锚：\n"
        + "\n".join(f"- {a}" for a in dict.fromkeys(anchors))
        + "\n\n写作要求：写 3–5 段横向对比：谁高谁低、差距多大、结合各公司业务给出定性解释；"
        "开头一句说明本页比较的指标与期间。只输出 JSON。"
    )
    try:
        output = client.chat_json(CONCEPT_PROMPT, user)
    except Exception as exc:
        return {"slug": slug, "status": "error", "reason": f"{type(exc).__name__}: {exc}"}

    template = str(output.get("body_markdown") or "")
    used = sorted(set(PLACEHOLDER.findall(template)), key=lambda m: int(m[1:]))
    if not used:
        return {"slug": slug, "status": "draft", "reason": "模型没有使用任何占位符"}
    by_marker = {item.marker: item for item in items}
    unknown = [m for m in used if m not in by_marker]
    if unknown:
        return {"slug": slug, "status": "draft", "reason": f"未知占位符 {unknown}"}

    page_rel = f"concepts/{slug}.md"
    claims = [
        Claim(
            page=page_rel,
            ordinal=int(by_marker[m].marker[1:]),
            kind="fact",
            fact_id=by_marker[m].fact_id,
            display_value=by_marker[m].display,
            unit=by_marker[m].unit,
            note=by_marker[m].text,
        )
        for m in used
    ]
    try:
        body = render_body(template, claims)
    except ValueError as exc:
        return {"slug": slug, "status": "draft", "reason": str(exc)}
    body = re.sub(r"(%\[\^c\d+\])%", r"\1", body)

    doc_list = [f["doc"] for f in facts_used]
    sources = [f"l0:{doc}" for doc in doc_list] + [
        f"l1:facts#{c.fact_id}" for c in claims if c.fact_id is not None
    ]
    meta = frontmatter.build(
        page_type="concept",
        title=str(output.get("title") or title),
        description=str(output.get("description") or description)[:80],
        sources=sources,
        company=",".join(sorted({f["doc"].split("_")[0] for f in facts_used})),
        period=period,
        tags=["概念", "跨公司对比", "自动编译"],
        status="draft",
    )
    if not frontmatter.has_enough_sources(meta):
        return {"slug": slug, "status": "draft", "reason": "来源不足两层（ADR-041）"}

    # 概念页底部链回各公司摘要（防孤儿页 + 提供下钻入口）。
    links = [
        f"- [[summaries/{doc}|{name} {doc.split('_')[1]} {doc.split('_')[2]}摘要]]"
        for doc, name in ((f["doc"], f["name"]) for f in facts_used)
        if page_path(vault_root, "summary", doc).is_file()
    ]
    if links:
        body = body.rstrip("\n") + "\n\n## 相关公司摘要\n" + "\n".join(dict.fromkeys(links)) + "\n"

    if dry_run:
        return {"slug": slug, "status": "dry-run", "body": body}

    claims_path = claims_db or (FINKB_ROOT / "data" / "wiki.duckdb")
    draft_path = write_page(vault_root, "concept", slug, meta, body, status="draft")
    with ClaimStore(claims_path) as store_claims:
        store_claims.delete_page(page_rel)
        store_claims.insert_many(claims)

    issues = lint_rules.page_issues(
        vault_root,
        page_rel,
        claims_db=claims_path,
        facts_db=FINKB_ROOT / "data" / "facts.duckdb",
        data_root=data_root,
        queries_path=FINKB_ROOT / "config" / "wiki_queries.yaml",
        facts_conn=store.conn,
    )
    if issues:
        return {"slug": slug, "status": "draft", "path": str(draft_path), "issues": [i.message for i in issues]}
    active_path = write_page(vault_root, "concept", slug, meta, body, status="active")
    draft_path.unlink(missing_ok=True)
    return {"slug": slug, "status": "active", "path": str(active_path)}
