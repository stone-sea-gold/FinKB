"""L2 lint：9 条规则（方案 P8 + ADR-041/043/044）。

规则 1–8 是结构与链接；规则 9 守「数字只由 SQL 计算」这条核心不变量。
所有规则都只读，``--fix`` 只会动规则 2（断链降级成纯文本），不做别的改写。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

from finkb.fact_engine.store import FactStore
from finkb.wiki import frontmatter, vault
from finkb.wiki.claims import ClaimStore, format_value, load_queries

WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
REF = re.compile(r"\[Ref:\s*([^#\]]+)#P(\d+)\]")
FOOTNOTE_USE = re.compile(r"\[\^(c\d+)\]")
FOOTNOTE_DEF = re.compile(r"^\[\^(c\d+)\]:.*$", re.MULTILINE)
NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?%?")
YEAR = re.compile(r"^(?:19|20)\d{2}$")
STOCK_CODE = re.compile(r"[（(]\d{6}[)）]")
DOC_ID = re.compile(r"\d{6}_\d{4}_(?:年报|半年报|一季报|三季报)")
PERIOD_TOKEN = re.compile(r"(?:19|20)\d{2}(?:Q[1-4](?:-Q[1-4])?|H[12])")
PAGE_TEXT = re.compile(r"第\s*\d+\s*页")
HEADING = re.compile(r"^#{1,6}\s")
TABLE_LINE = re.compile(r"^\s*\|")
FENCE = re.compile(r"^```")
FOOTNOTE_DEF_LINE = re.compile(r"^\[\^c\d+\]:")
BOLD_ONLY = re.compile(r"^\*\*[^*]+\*\*$")


@dataclass(frozen=True)
class Issue:
    rule: str
    page: str
    message: str
    severity: str = "error"

    def as_row(self) -> str:
        return f"| {self.rule} | `{self.page}` | {self.message} |"


def _page_rel(root: Path, path: Path) -> str:
    """草稿页返回逻辑路径（去掉 ``_draft/``），claims 按逻辑路径记账。"""
    rel = path.relative_to(root).as_posix()
    return rel[len("_draft/"):] if rel.startswith("_draft/") else rel


def _all_pages(root: Path) -> list[vault.Page]:
    return vault.iter_pages(root, include_draft=True)


def _active_pages(root: Path) -> list[vault.Page]:
    return [p for p in vault.iter_pages(root) if p.meta.get("status") == "active"]


def _resolve_link(root: Path, target: str) -> Path | None:
    target = target.strip()
    candidates = [
        root / f"{target}.md",
        root / target,
        root / "_draft" / f"{target}.md",
        root / "_draft" / target,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if "/" not in target:
        for base in (root, root / "_draft"):
            for path in base.glob(f"*/{target}.md"):
                return path
    return None


def rule1_broken_links(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for page in _all_pages(root):
        for match in WIKILINK.finditer(page.body):
            if _resolve_link(root, match.group(1)) is None:
                issues.append(Issue("R1", _page_rel(root, page.path), f"断链 [[{match.group(1)}]]"))
    return issues


def fix_broken_links(root: Path) -> int:
    """把断链降级成纯文本（保留 label），返回修改页数。"""
    changed = 0
    for page in _all_pages(root):
        if page.path.name == "index.md":
            continue

        def replace(match: re.Match[str]) -> str:
            if _resolve_link(root, match.group(1)) is not None:
                return match.group(0)
            return match.group(2) or match.group(1)

        new_body = WIKILINK.sub(replace, page.body)
        if new_body != page.body:
            vault.atomic_write(page.path, frontmatter.dump_frontmatter(page.meta) + "\n" + new_body.lstrip("\n"))
            changed += 1
    return changed


def rule2_ghost_links(root: Path) -> list[Issue]:
    """--fix 前的提示：哪些断链会被降级。"""
    return [
        Issue("R2", _page_rel(root, page.path), f"可修复断链 [[{m.group(1)}]]", "warning")
        for page in _all_pages(root)
        for m in WIKILINK.finditer(page.body)
        if _resolve_link(root, m.group(1)) is None
    ]


def rule3_orphans(root: Path) -> list[Issue]:
    pages = _active_pages(root)
    inbound: dict[Path, int] = {page.path: 0 for page in pages}
    for page in pages:
        for match in WIKILINK.finditer(page.body):
            target = _resolve_link(root, match.group(1))
            if target in inbound and target != page.path:
                inbound[target] += 1
    return [
        Issue("R3", _page_rel(root, page.path), "孤儿页：没有任何页面链到它", "warning")
        for page in pages
        if inbound[page.path] == 0
    ]


def rule4_missing_entries(root: Path, scope_path: Path) -> list[Issue]:
    issues: list[Issue] = []
    for doc_id in vault.registered_doc_ids(scope_path):
        if not (root / "summaries" / f"{doc_id}.md").is_file() and not (
            root / "_draft" / "summaries" / f"{doc_id}.md"
        ).is_file():
            issues.append(Issue("R4", "summaries", f"登记文档没有 Summary 页：{doc_id}"))
    return issues


def rule5_index_sync(root: Path) -> list[Issue]:
    path = root / "index.md"
    if not path.is_file():
        return [Issue("R5", "index.md", "缺少 index.md")]
    expected = vault.render_index(vault.iter_pages(root))
    actual = path.read_text(encoding="utf-8")
    if actual.rstrip() != expected.rstrip():
        return [Issue("R5", "index.md", "index.md 与页面集合不同步（跑 python -m finkb.wiki index）")]
    return []


def rule6_frontmatter(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for page in _all_pages(root):
        for problem in frontmatter.validate(page.meta):
            issues.append(Issue("R6", _page_rel(root, page.path), problem))
    return issues


def rule7_okf_fields(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for page in _all_pages(root):
        for key in ("type", "description"):
            if not str(page.meta.get(key) or "").strip():
                issues.append(Issue("R7", _page_rel(root, page.path), f"缺少 OKF 必填字段 {key}"))
    return issues


def _pages_of_doc(data_root: Path, doc: str) -> set[int] | None:
    doc_dir = data_root / "parsed" / doc
    report = doc_dir / "report.json"
    anchored = doc_dir / "page_anchored.md"
    if not report.is_file() or not anchored.is_file():
        return None
    total = int(json.loads(report.read_text(encoding="utf-8")).get("pages") or 0)
    found = {int(n) for n in re.findall(r'"page":\s*(\d+)', anchored.read_text(encoding="utf-8"))}
    return {n for n in found if 1 <= n <= total}


def rule8_anchors(root: Path, data_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    cache: dict[str, set[int] | None] = {}
    for page in _all_pages(root):
        for match in REF.finditer(page.body):
            doc, number = match.group(1).strip(), int(match.group(2))
            if doc not in cache:
                cache[doc] = _pages_of_doc(data_root, doc)
            pages = cache[doc]
            if pages is None:
                issues.append(Issue("R8", _page_rel(root, page.path), f"锚点文档不存在：{doc}"))
            elif number not in pages:
                issues.append(Issue("R8", _page_rel(root, page.path), f"锚点页不存在：{doc}#P{number}"))
    return issues


def _norm_number(text: str) -> str:
    return text.replace(",", "").strip()


def _sentence_numbers(sentence: str) -> list[str]:
    sentence = PERIOD_TOKEN.sub("", sentence)
    sentence = PAGE_TEXT.sub("", sentence)
    sentence = DOC_ID.sub("", sentence)
    sentence = STOCK_CODE.sub("", sentence)
    sentence = WIKILINK.sub("", sentence)
    sentence = REF.sub("", sentence)
    sentence = FOOTNOTE_USE.sub("", sentence)
    return [n for n in NUMBER.findall(sentence) if not YEAR.match(_norm_number(n))]


def _quote_found(data_root: Path, anchor: str | None, number: str) -> bool:
    if not anchor or "#P" not in anchor:
        return False
    doc = anchor.split("#P", 1)[0]
    path = data_root / "parsed" / doc / "page_anchored.md"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    return _norm_number(number) in _norm_number(text)


def rule9_claims(
    root: Path,
    *,
    claims_db: Path,
    facts_db: Path | None = None,
    data_root: Path,
    queries_path: Path,
    facts_conn: duckdb.DuckDBPyConnection | None = None,
) -> list[Issue]:
    """数字覆盖 + 三类 claim 重验（ADR-044）。

    ``facts_conn`` 用于编译期复用已打开的写连接：DuckDB 同一文件不能同时以
    只读和读写两种配置打开。
    """
    if not claims_db.is_file():
        return []
    issues: list[Issue] = []
    queries = load_queries(queries_path)
    owns_conn = facts_conn is None
    if facts_conn is None:
        if facts_db is None:
            return []
        facts_conn = duckdb.connect(str(facts_db), read_only=True)
    claims = ClaimStore(claims_db)
    try:
        for page in _all_pages(root):
            if page.meta.get("type") == "entity":
                continue  # 实体页只列关系与页锚，名称里的数字是标识符，不承载数值主张
            rel = _page_rel(root, page.path)
            body = page.body
            defs = {m.group(1) for m in FOOTNOTE_DEF.finditer(body)}
            body_without_defs = FOOTNOTE_DEF.sub("", body)
            page_claims = {c.marker: c for c in claims.for_page(rel)}

            for marker in sorted(defs | set(FOOTNOTE_USE.findall(body_without_defs))):
                if marker not in page_claims:
                    issues.append(Issue("R9", rel, f"脚注 {marker} 没有对应 claim"))
            for marker in page_claims:
                if marker not in defs:
                    issues.append(Issue("R9", rel, f"claim {marker} 没有脚注定义"))

            for claim in page_claims.values():
                if claim.kind == "fact":
                    row = facts_conn.execute(
                        "SELECT value_norm, norm_unit FROM facts WHERE fact_id = ?",
                        [claim.fact_id],
                    ).fetchone()
                    if row is None:
                        issues.append(Issue("R9", rel, f"{claim.marker} 指向不存在的 facts #{claim.fact_id}"))
                        continue
                    expected = format_value(float(row[0]), str(row[1] or ""))
                    if _norm_number(expected) != _norm_number(claim.display_value):
                        issues.append(Issue("R9", rel, f"{claim.marker} 显示值与 facts 不符：{claim.display_value} != {expected}"))
                elif claim.kind == "segment":
                    row = facts_conn.execute(
                        "SELECT value_norm, unit FROM segment_facts WHERE segment_id = ?",
                        [claim.segment_id],
                    ).fetchone()
                    if row is None:
                        issues.append(Issue("R9", rel, f"{claim.marker} 指向不存在的 segment_facts #{claim.segment_id}"))
                        continue
                    expected = format_value(float(row[0]), str(row[1] or ""))
                    if _norm_number(expected) != _norm_number(claim.display_value):
                        issues.append(
                            Issue("R9", rel, f"{claim.marker} 显示值与 segment_facts 不符：{claim.display_value} != {expected}")
                        )
                elif claim.kind == "query":
                    spec = queries.get(claim.query_id or "")
                    if spec is None:
                        issues.append(Issue("R9", rel, f"{claim.marker} 引用了未定义查询 {claim.query_id}"))
                        continue
                    values = [claim.params.get(name) for name in spec.get("params") or []]
                    try:
                        row = facts_conn.execute(spec["sql"], values).fetchone()
                    except Exception as exc:  # noqa: BLE001 - 查询本身坏了要报出来
                        issues.append(Issue("R9", rel, f"{claim.marker} 查询失败：{exc}"))
                        continue
                    expected = format_value(float(row[0]), str(spec.get("unit") or "")) if row and row[0] is not None else None
                    if expected is None or _norm_number(expected) != _norm_number(claim.display_value):
                        issues.append(Issue("R9", rel, f"{claim.marker} 查询结果与显示值不符"))
                elif claim.kind == "quote":
                    if not _quote_found(data_root, claim.anchor, claim.display_value):
                        issues.append(Issue("R9", rel, f"{claim.marker} 引文数字在 {claim.anchor} 找不到"))

            for line in body_without_defs.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                for sentence in re.split(r"(?<=[。！？!?])|\n", line):
                    numbers = _sentence_numbers(sentence)
                    if not numbers:
                        continue
                    markers = FOOTNOTE_USE.findall(sentence)
                    if not markers:
                        issues.append(Issue("R9", rel, f"数字没有 claim 覆盖：{sentence.strip()[:60]}"))
                        continue
                    allowed = {
                        _norm_number(page_claims[m].display_value)
                        for m in markers
                        if m in page_claims
                    }
                    for number in numbers:
                        if _norm_number(number) not in allowed:
                            issues.append(Issue("R9", rel, f"数字与 claim 不符：{number}（{sentence.strip()[:40]}）"))
    finally:
        claims.close()
        if owns_conn:
            facts_conn.close()
    return issues




def _paragraphs(body: str) -> list[str]:
    """按空行切段；跳过标题、表格、代码块、脚注定义。"""
    paragraphs: list[str] = []
    current: list[str] = []
    in_table = False
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append("\n".join(current))
                current = []
            in_table = False
            continue
        if FENCE.match(stripped):
            if current:
                paragraphs.append("\n".join(current))
                current = []
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if HEADING.match(stripped) or TABLE_LINE.match(stripped):
            if current:
                paragraphs.append("\n".join(current))
                current = []
            in_table = True
            continue
        if in_table or FOOTNOTE_DEF_LINE.match(stripped):
            continue
        current.append(stripped)
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def rule10_narrative_anchors(root: Path) -> list[Issue]:
    """规则 10：定性段落必须带页锚（方案 §9.4）。

    只检查「既没有 claim 脚注、也没有 [Ref: doc#Pn]」的正文段落；标题、表格、
    导航链接、脚注定义不算定性主张。
    """
    issues: list[Issue] = []
    for page in vault.iter_pages(root):
        if page.meta.get("type") == "entity":
            continue  # 实体页是关系索引，正文是名称列表，不承担定性叙述
        rel = _page_rel(root, page.path)
        for paragraph in _paragraphs(page.body):
            text = paragraph.strip()
            if not text:
                continue
            if FOOTNOTE_USE.search(text) or REF.search(text):
                continue
            plain = WIKILINK.sub("", text).strip(" -：:")
            if not plain:
                continue
            if BOLD_ONLY.match(text):  # 「**小标题**」是排版，不是定性主张
                continue
            if WIKILINK.search(text) and len(plain) <= 8:  # 「返回摘要：[[...]]」这类导航
                continue
            if plain.startswith("本页"):  # 「本页比较……」是页面级说明，不承载对公司的定性主张
                continue
            if re.search(r"如下[：:]?$", plain):  # 「核心财务指标如下：」是表格引导语
                continue
            snippet = re.sub(r"\s+", " ", text)[:50]
            issues.append(Issue("R10", rel, f"定性段落缺少页锚：{snippet}", "warning"))
    return issues

def lint_vault(
    root: Path | str,
    *,
    data_root: Path | str,
    claims_db: Path | str,
    facts_db: Path | str,
    scope_path: Path | str,
    queries_path: Path | str,
    fix: bool = False,
) -> list[Issue]:
    root = Path(root)
    data_root = Path(data_root)
    issues: list[Issue] = []
    if fix:
        fix_broken_links(root)
    issues += rule1_broken_links(root)
    issues += rule2_ghost_links(root) if not fix else []
    issues += rule3_orphans(root)
    issues += rule4_missing_entries(root, Path(scope_path))
    issues += rule5_index_sync(root)
    issues += rule6_frontmatter(root)
    issues += rule7_okf_fields(root)
    issues += rule8_anchors(root, data_root)
    issues += rule9_claims(
        root,
        claims_db=Path(claims_db),
        facts_db=Path(facts_db),
        data_root=data_root,
        queries_path=Path(queries_path),
    )
    issues += rule10_narrative_anchors(root)
    return issues


def render_report(issues: list[Issue]) -> str:
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity != "error"]
    lines = [
        "# L2 lint 报告\n",
        f"- 错误：{len(errors)}",
        f"- 警告：{len(warnings)}",
        "",
        "| 规则 | 页面 | 说明 |",
        "|---|---|---|",
    ]
    lines += [issue.as_row() for issue in issues]
    return "\n".join(lines) + "\n"


def page_issues(
    root: Path,
    page: str,
    *,
    claims_db: Path,
    facts_db: Path | None = None,
    data_root: Path,
    queries_path: Path,
    facts_conn: duckdb.DuckDBPyConnection | None = None,
) -> list[Issue]:
    """只取某一页的内容类问题（R1/R8/R9），用于编译后决定能否转正。"""
    issues: list[Issue] = []
    issues += [i for i in rule1_broken_links(root) if i.page == page]
    issues += [i for i in rule8_anchors(root, data_root) if i.page == page]
    issues += [i for i in rule10_narrative_anchors(root) if i.page == page]
    issues += [
        i
        for i in rule9_claims(
            root,
            claims_db=claims_db,
            facts_db=facts_db,
            data_root=data_root,
            queries_path=queries_path,
            facts_conn=facts_conn,
        )
        if i.page == page
    ]
    return issues
