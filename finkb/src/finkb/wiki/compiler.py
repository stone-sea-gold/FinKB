"""L2 单文档编译器：把 L1 事实编译成 wiki 页面（ADR-044 / P7）。

模型只负责「写叙述 + 放占位符」：正文里的每个数字都写成 ``{{cN}}``，
``cN`` 由代码在编译前从 facts / segment_facts / 命名 SQL 生成目录，
渲染时再换成真实数值并写 claims 账本。因此模型全程看不到、也写不出数字。

产物先落 ``_draft/``；只有同时满足「来源 ≥2 层」且 lint 内容规则（R1/R8/R9）
无错时才转正到正式目录，否则停在草稿区等人工处理。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from finkb.fact_engine.extractor import LLMClient, flow_period
from finkb.fact_engine.selector import FINKB_ROOT
from finkb.fact_engine.store import FactStore
from finkb.lint import rules as lint_rules
from finkb.wiki import frontmatter
from finkb.wiki.claims import (
    PLACEHOLDER,
    Claim,
    ClaimStore,
    claim_from_fact,
    claim_from_query,
    load_queries,
    render_body,
)
from finkb.wiki.vault import TYPE_DIRS, page_path, read_page, write_page

DEFAULT_DATA_ROOT = FINKB_ROOT / "data"
DEFAULT_VAULT = FINKB_ROOT / "wiki_vault"

SUMMARY_METRICS = (
    "revenue",
    "net_profit",
    "net_profit_deducted",
    "operating_profit",
    "total_profit",
    "eps_basic",
    "roe_weighted",
    "ocf",
    "total_assets",
    "net_assets",
    "total_liabilities",
    "operating_cost",
    "rd_expense",
    "cash",
    "inventory",
)

CASH_METRICS = (
    "ocf",
    "icf",
    "fcf",
    "net_cash_increase",
    "cash",
    "short_term_borrowings",
    "long_term_borrowings",
    "bonds_payable",
    "lease_liabilities",
)

RD_METRICS = ("rd_expense",)

# 分地区表里「境外口径」的常见列名（与 config/wiki_queries.yaml 的过滤条件保持一致）。
OVERSEAS_SEGMENTS = frozenset({"海外", "境外", "国外", "境外地区", "海外地区"})

STOCK_METRICS = frozenset(
    {
        "total_assets",
        "net_assets",
        "total_liabilities",
        "total_equity",
        "cash",
        "inventory",
        "accounts_receivable",
        "short_term_borrowings",
        "long_term_borrowings",
        "bonds_payable",
        "lease_liabilities",
        "current_assets",
        "current_liabilities",
    }
)

SYSTEM_PROMPT = """你是财务知识库的编辑。你只能使用用户给出的「claim 目录」里的占位符来写数字。

硬性规则：
1. 正文里任何数值都必须写成占位符 {{cN}}，不得直接写阿拉伯数字。
   金额单位写在占位符后面，例如「{{c1}} 亿元」；百分数按规则 4。
   例外：年份（如 2025 年）、页码引用 [Ref: 文档#P12] 可以写。
2. 只能使用目录里出现过的 cN，不得编造。
3. 每一段定性陈述都必须带页锚（每段至少一条），格式 [Ref: 文档#P页码]，页码只能取自用户给的「可用页锚」；
   优先引用该段涉及的 claim 旁边的原文页码（目录里标注了「原文第 N 页」）。
4. 百分数占位符的显示值已经带 %，不要再在占位符后面补 %（如毛利率写成 {{c11}}，不要写 {{c11}}%）。
5. 不要输出 Markdown 分隔线 ---，不要输出 frontmatter。
6. 只输出一个 JSON 对象：{"title": "...", "description": "...", "body_markdown": "..."}。
   description 一句话，不超过 60 字。
7. 占位符必须写成双花括号 {{cN}}；写单花括号 {cN} 不会渲染，视为错误。
8. 不得自行计算任何数字：差值、增速、占比、百分数、合计都不行，只能引用目录里已有的数值。
   叙述差异时用「明显高于/低于」这类文字，不要写算出来的新数字。
9. description 是给人看的一句话摘要，不要出现占位符 {{cN}}，也不要写具体数字。"""


@dataclass
class CatalogItem:
    marker: str
    kind: str
    text: str
    display: str
    unit: str = ""
    fact_id: int | None = None
    segment_id: int | None = None
    query_id: str | None = None
    params: dict = field(default_factory=dict)
    page: int | None = None

    def line(self) -> str:
        unit = "" if self.display.endswith("%") else self.unit
        where = f"（原文第 {self.page} 页）" if self.page is not None else ""
        return f"{{{{{self.marker}}}}} = {self.text} = {self.display}{unit}{where}"


def _fact_rows(store: FactStore, doc_id: str) -> list[dict]:
    cursor = store.conn.execute("SELECT * FROM facts WHERE source_doc = ?", [doc_id])
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _segment_rows(store: FactStore, doc_id: str) -> list[dict]:
    cursor = store.conn.execute(
        "SELECT segment_id, dimension, segment_name, metric, value_norm, unit, page_num FROM segment_facts "
        "WHERE source_doc = ? ORDER BY dimension, segment_name, metric",
        [doc_id],
    )
    return [
        {
            "segment_id": row[0],
            "dimension": row[1],
            "segment_name": row[2],
            "metric": row[3],
            "value_norm": row[4],
            "unit": row[5],
            "page_num": row[6],
        }
        for row in cursor.fetchall()
    ]


def _period_choices(metric: str, period: str) -> tuple[str, ...]:
    """三季报的流量指标记在「年初至报告期末」（Q1-Q3），存量指标记在 Q3。

    返回按优先级排列的候选期间：流量优先累计期，存量优先 doc_period。
    """
    flow = flow_period(period)
    if flow == period:
        return (period,)
    return (period,) if metric in STOCK_METRICS else (flow, period)


def build_catalog(
    store: FactStore, doc_id: str, metrics: tuple[str, ...], period: str
) -> list[CatalogItem]:
    """按固定指标顺序生成 claim 目录；只取本期列（年报摘要表里还有上年/前年列）。"""
    by_metric: dict[str, dict[str, dict]] = {}
    for row in _fact_rows(store, doc_id):
        by_metric.setdefault(row["metric_id"], {})[str(row.get("period"))] = row
    items: list[CatalogItem] = []
    for metric in metrics:
        rows = by_metric.get(metric) or {}
        fact = next((rows[p] for p in _period_choices(metric, period) if p in rows), None)
        if fact is None or fact.get("value_norm") is None:
            continue
        ordinal = len(items) + 1
        claim = claim_from_fact("", ordinal, fact)
        items.append(
            CatalogItem(
                marker=claim.marker,
                kind="fact",
                text=f"{metric}",
                display=claim.display_value,
                unit=claim.unit,
                fact_id=claim.fact_id,
                page=int(fact["page_num"]) if fact.get("page_num") is not None else None,
            )
        )
    return items


def add_segments(items: list[CatalogItem], segments: list[dict]) -> None:
    label = {"product": "分产品", "industry": "分行业", "region": "分地区"}
    metric_label = {
        "segment_revenue": "营业收入",
        "segment_cost": "营业成本",
        "segment_gross_margin": "毛利率",
    }
    for row in segments:
        ordinal = len(items) + 1
        marker = f"c{ordinal}"
        value = float(row["value_norm"])
        unit = str(row["unit"] or "")
        display = f"{value:.2f}%" if unit == "%" else f"{value:,.2f}"
        items.append(
            CatalogItem(
                marker=marker,
                kind="segment",
                text=f"{label.get(row['dimension'], row['dimension'])}-{row['segment_name']}-{metric_label.get(row['metric'], row['metric'])}",
                display=display,
                unit="" if unit == "%" else unit,
                segment_id=int(row["segment_id"]),
                page=int(row["page_num"]) if row.get("page_num") is not None else None,
            )
        )


def _try_add_query(items: list[CatalogItem], store: FactStore, query_id: str, params: dict) -> None:
    """加命名 SQL claim；该口径查不到结果时跳过（如银行没有营业成本）。"""
    try:
        add_query(items, store, query_id, params)
    except ValueError:
        pass


def add_query(items: list[CatalogItem], store: FactStore, query_id: str, params: dict) -> None:
    ordinal = len(items) + 1
    claim = claim_from_query("", ordinal, query_id, params, facts_db=store.conn)
    items.append(
        CatalogItem(
            marker=claim.marker,
            kind="query",
            text=query_id,
            display=claim.display_value,
            unit=claim.unit,
            query_id=query_id,
            params=params,
        )
    )


def _to_claim(item: CatalogItem, page: str) -> Claim:
    ordinal = int(item.marker[1:])
    return Claim(
        page=page,
        ordinal=ordinal,
        kind=item.kind,
        fact_id=item.fact_id,
        segment_id=item.segment_id,
        query_id=item.query_id,
        params=item.params,
        display_value=item.display,
        unit=item.unit,
        note=item.text,
    )


def report_kind(doc_id: str) -> str:
    """``002594_2025_半年报`` → ``半年报``。"""
    return doc_id.split("_", 2)[2]


def _page_specs(doc_id: str, company: str, period: str) -> list[dict]:
    kind = report_kind(doc_id)
    year = period[:4]
    head = f"{company} {year} {kind}"
    return [
        {
            "page_type": "summary",
            "slug": doc_id,
            "title": f"{head}摘要",
            "desc": f"{head}总览：业务、收入与利润、现金流、资产结构与研发投入。",
            "catalog": SUMMARY_METRICS,
            "segments": False,
            "cash": False,
            "brief": "写一段 4–6 段的总览：公司业务、收入与利润、现金流、资产结构、研发投入。",
        },
        {
            "page_type": "exploration",
            "slug": f"{doc_id}-核心财务指标",
            "title": f"{head}核心财务指标",
            "desc": f"{head}的核心财务指标与经营质量解读。",
            "catalog": SUMMARY_METRICS,
            "segments": False,
            "cash": False,
            "brief": "写一份核心财务指标解读：先列一张 Markdown 表（指标 | 数值），再写 2–3 段趋势与质量分析。",
        },
        {
            "page_type": "exploration",
            "slug": f"{doc_id}-分产品结构",
            "title": f"{head}分产品与分地区结构",
            "desc": f"{head}的分产品、分行业与分地区收入结构。",
            "catalog": (),
            "segments": True,
            "cash": False,
            "brief": "写一份业务结构解读：用分产品/分行业/分地区的 claim 写 2–3 段，说明收入构成与毛利率差异。",
        },
        {
            "page_type": "exploration",
            "slug": f"{doc_id}-现金流与资本结构",
            "title": f"{head}现金流与资本结构",
            "desc": f"{head}的经营、投资、筹资现金流与有息负债结构。",
            "catalog": CASH_METRICS,
            "segments": False,
            "cash": True,
            "brief": "写一份现金流与资本结构解读：经营/投资/筹资现金流、现金余额、有息负债结构，2–3 段。",
        },
        {
            "page_type": "exploration",
            "slug": f"{doc_id}-研发投入",
            "title": f"{head}研发投入",
            "desc": f"{head}的研发费用规模与研发强度。",
            "catalog": RD_METRICS,
            "segments": False,
            "cash": False,
            "rd": True,
            "brief": "写一份研发投入解读：研发费用与研发强度（占营收比重），2–3 段；数据缺失时不要写数字。",
        },
        {
            "page_type": "exploration",
            "slug": f"{doc_id}-海外收入",
            "title": f"{head}海外收入",
            "desc": f"{head}的境外收入、成本与毛利率。",
            "catalog": (),
            "segments": False,
            "cash": False,
            "overseas": True,
            "brief": "写一份海外业务解读：境外/海外收入与毛利率，2–3 段；没有境外口径数据时不要写数字。",
        },
    ]


def compile_document(
    client: LLMClient,
    store: FactStore,
    doc_id: str,
    *,
    company: str,
    period: str,
    vault_root: Path = DEFAULT_VAULT,
    claims_db: Path | None = None,
    data_root: Path = DEFAULT_DATA_ROOT,
    dry_run: bool = False,
    only: set[str] | None = None,
) -> list[dict]:
    """编译一份文档的 1 个摘要页 + 3 个探针页，返回每页的编译结果。"""
    claims_path = claims_db or (FINKB_ROOT / "data" / "wiki.duckdb")
    segments = _segment_rows(store, doc_id)
    queries = load_queries()
    results: list[dict] = []

    specs = _page_specs(doc_id, company, period)
    if only is not None:
        specs = [spec for spec in specs if spec["slug"] in only]
    for spec in specs:
        items = build_catalog(store, doc_id, spec["catalog"], period)
        if spec["segments"]:
            add_segments(items, segments)
        # facts.company 存的是股票代码，页面展示用公司名，查询参数要用代码。
        code = doc_id.split("_")[0]
        flow = flow_period(period)
        if spec.get("rd"):
            if "rd_ratio" in queries:
                _try_add_query(items, store, "rd_ratio", {"company": code, "period": flow})
        elif spec.get("overseas"):
            # 用分地区表里的境外口径原始条目：既提供数字，也提供页锚（l1:segments）。
            add_segments(
                items,
                [
                    r
                    for r in segments
                    if r["dimension"] == "region" and r["segment_name"] in OVERSEAS_SEGMENTS
                ],
            )
        elif not spec["segments"]:
            if "debt_ratio" in queries:
                _try_add_query(items, store, "debt_ratio", {"company": code, "period": period})
            for query_id in ("gross_margin", "net_margin", "rd_ratio"):
                if query_id in queries:
                    _try_add_query(items, store, query_id, {"company": code, "period": flow})
        if not items:
            results.append({"slug": spec["slug"], "status": "skipped", "reason": "没有可用 claim"})
            continue

        catalog_text = "\n".join(item.line() for item in items)
        anchors = _available_anchors(data_root, doc_id)
        # claim 所在页最相关，优先给模型；再补文档前几页兜底。
        relevant = [f"{doc_id}#P{item.page}" for item in items if item.page is not None]
        if spec.get("overseas"):
            relevant = [
                f"{doc_id}#P{r['page_num']}"
                for r in segments
                if r["dimension"] == "region"
                and r["segment_name"] in OVERSEAS_SEGMENTS
                and r.get("page_num") is not None
            ] + relevant
        anchors = list(dict.fromkeys(relevant + anchors))
        user = (
            f"文档：{doc_id}\n公司：{company}\n期间：{period}\n\n"
            f"claim 目录（正文数字只能用这些占位符）：\n{catalog_text}\n\n"
            f"可用页锚：{', '.join(anchors[:12])}\n\n"
            f"写作要求：{spec['brief']}\n"
        )
        try:
            output = client.chat_json(SYSTEM_PROMPT, user)
        except Exception as exc:
            results.append({"slug": spec["slug"], "status": "error", "reason": f"{type(exc).__name__}: {exc}"})
            continue

        body_template = str(output.get("body_markdown") or "")
        used = sorted(set(PLACEHOLDER.findall(body_template)), key=lambda m: int(m[1:]))
        if not used:
            results.append({"slug": spec["slug"], "status": "draft", "reason": "模型没有使用任何占位符"})
            continue
        by_marker = {item.marker: item for item in items}
        unknown = [m for m in used if m not in by_marker]
        if unknown:
            results.append({"slug": spec["slug"], "status": "draft", "reason": f"未知占位符 {unknown}"})
            continue

        page_rel = f"{TYPE_DIRS[spec['page_type']]}/{spec['slug']}.md"
        page_claims = [_to_claim(by_marker[m], page_rel) for m in used]
        try:
            body = render_body(body_template, page_claims)
        except ValueError as exc:
            results.append({"slug": spec["slug"], "status": "draft", "reason": str(exc)})
            continue

        body = re.sub(r"(%\[\^c\d+\])%", r"\1", body)

        fact_ids = [c.fact_id for c in page_claims if c.fact_id is not None]
        segment_ids = [c.segment_id for c in page_claims if c.segment_id is not None]
        sources = (
            [f"l0:{doc_id}"]
            + [f"l1:facts#{fid}" for fid in dict.fromkeys(fact_ids)]
            + [f"l1:segments#{sid}" for sid in dict.fromkeys(segment_ids)]
        )
        title = str(output.get("title") or spec["title"])
        description = str(output.get("description") or spec["brief"])[:80]
        if "{{" in title:
            title = spec["title"]
        if "{{" in description:
            description = str(spec.get("desc") or spec["brief"])[:80]
        meta = frontmatter.build(
            page_type=spec["page_type"],
            title=title,
            description=description,
            sources=sources,
            company=company,
            period=period,
            tags=[report_kind(doc_id), "自动编译"],
            status="draft",
        )

        if not frontmatter.has_enough_sources(meta):
            results.append({"slug": spec["slug"], "status": "draft", "reason": "来源不足两层（ADR-041）"})
            continue

        if dry_run:
            results.append({"slug": spec["slug"], "status": "dry-run", "body": body})
            continue

        draft_path = write_page(vault_root, spec["page_type"], spec["slug"], meta, body, status="draft")
        with ClaimStore(claims_path) as claims:
            claims.delete_page(page_rel)
            claims.insert_many(page_claims)
        # 旧 active 页与本次 claims 不再一致；先撤掉，避免同 rel 两页并存干扰校验。
        page_path(vault_root, spec["page_type"], spec["slug"]).unlink(missing_ok=True)

        issues = lint_rules.page_issues(
            vault_root,
            page_rel,
            claims_db=claims_path,
            facts_db=FINKB_ROOT / "data" / "facts.duckdb",
            data_root=data_root,
            queries_path=FINKB_ROOT / "config" / "wiki_queries.yaml",
            facts_conn=store.conn,
        )
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            results.append(
                {"slug": spec["slug"], "status": "draft", "path": str(draft_path), "issues": [i.message for i in errors]}
            )
            continue

        active_path = write_page(vault_root, spec["page_type"], spec["slug"], meta, body, status="active")
        draft_path.unlink(missing_ok=True)
        results.append({"slug": spec["slug"], "status": "active", "path": str(active_path)})

    by_slug = {spec["slug"]: spec for spec in specs}
    for item in results:
        if item["status"] != "active":
            continue
        page = read_page(Path(item["path"]))
        body = _cross_links(page.body, by_slug[item["slug"]], doc_id, vault_root)
        if body != page.body:
            write_page(vault_root, page.meta["type"], item["slug"], page.meta, body, status="active")
    return results


def _cross_links(body: str, spec: dict, doc_id: str, root: Path) -> str:
    """摘要页链到已存在的探针，探针页链回摘要；顺带消掉 lint 的孤儿告警。"""
    def exists(page_type: str, slug: str) -> bool:
        return page_path(root, page_type, slug).is_file() or page_path(root, page_type, slug, status="draft").is_file()

    probes = [
        ("核心财务指标", f"{doc_id}-核心财务指标"),
        ("分产品结构", f"{doc_id}-分产品结构"),
        ("现金流与资本结构", f"{doc_id}-现金流与资本结构"),
        ("研发投入", f"{doc_id}-研发投入"),
        ("海外收入", f"{doc_id}-海外收入"),
    ]
    if spec["page_type"] == "summary":
        links = [
            f"- [[explorations/{slug}|{label}]]"
            for label, slug in probes
            if exists("exploration", slug)
        ]
        if not links:
            return body
        return body.rstrip("\n") + "\n\n## 延伸阅读\n" + "\n".join(links) + "\n"
    if not exists("summary", doc_id):
        return body
    return body.rstrip("\n") + f"\n\n返回摘要：[[summaries/{doc_id}|{doc_id}]]\n"


def _available_anchors(data_root: Path, doc_id: str) -> list[str]:
    """从 page_anchored.md 抽前几页的页锚，供模型写定性引用。"""
    path = data_root / "parsed" / doc_id / "page_anchored.md"
    if not path.exists():
        return [f"{doc_id}#P1"]
    pages = re.findall(r'"page":\s*(\d+)', path.read_text(encoding="utf-8")[:200000])
    ordered = list(dict.fromkeys(pages))
    return [f"{doc_id}#P{p}" for p in ordered[:20]] or [f"{doc_id}#P1"]
