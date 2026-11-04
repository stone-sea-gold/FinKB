"""附注锚点：指标 → 报表页 + 附注章节页（S2）。

年报里同一个科目会出现两次：合并报表主表一行，附注里一段明细。回答「为什么
研发费用涨了」这类问题时，只给主表页码不够，要能落到附注章节。这里做两件事：

1. **报表页锚**：直接来自 facts 的 ``source_doc + page_num``，36 个指标都有；
2. **附注锚**：扫 ``page_anchored.md`` 的 META ``section``（条目级标题，如
   「7、存货（续）」「45、营业收入和营业成本」），用 metric_dict 的别名匹配。

结果落 ``note_anchors`` 表，供 :func:`finkb.ontology.operators.note_drill_down`
和 S4 问答引用。附注不是每个科目都单列（净利润、经营现金流等主表合计行），
所以附注锚覆盖天然低于报表页锚，不能拿 100% 当验收线。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

from finkb.fact_engine.selector import FINKB_ROOT, norm_label

META_RE = re.compile(r"<!--\s*META:\s*(\{.*?\})\s*-->")
GENERIC_ALIASES = {"收入", "利润", "现金", "资产", "负债", "权益", "费用", "成本", "金额"}


def iter_sections(doc_dir: Path):
    """按 META 块切 ``page_anchored.md``，产出 (page, section, text)。"""
    file = doc_dir / "page_anchored.md"
    if not file.exists():
        return
    page = 0
    section = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if page:
            yield page, section, text
        buffer = []

    for line in file.read_text(encoding="utf-8").splitlines():
        match = META_RE.match(line.strip())
        if match:
            yield from flush()
            try:
                meta = json.loads(match.group(1))
            except json.JSONDecodeError:
                meta = {}
            page = int(meta.get("page") or 0)
            section = str(meta.get("section") or "")
            continue
        buffer.append(line)
    yield from flush()


def _alias_index(metric_dict_path: Path | None = None) -> list[tuple[str, str]]:
    """返回 (metric_id, 归一化别名) 列表，长别名优先，避免短词抢先。"""
    path = metric_dict_path or (FINKB_ROOT / "config" / "metric_dict.yaml")
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    pairs: list[tuple[str, str]] = []
    for metric in data.get("metrics") or []:
        metric_id = str(metric.get("metric_id") or "")
        if not metric_id:
            continue
        names = [metric.get("metric_name") or ""] + list(metric.get("aliases") or [])
        for name in names:
            alias = norm_label(name)
            if len(alias) < 2 or alias in GENERIC_ALIASES:
                continue
            pairs.append((metric_id, alias))
    pairs.sort(key=lambda item: -len(item[1]))
    return pairs


def _page_sections(doc_dir: Path) -> dict[int, str]:
    """每页取第一个非空 section，用于给报表页锚补章节名。"""
    mapping: dict[int, str] = {}
    for page, section, _text in iter_sections(doc_dir):
        if section and page not in mapping:
            mapping[page] = section
    return mapping


def build(
    store,
    parsed_root: str | Path | None = None,
    metric_dict_path: str | Path | None = None,
) -> dict:
    """重建 note_anchors 表；返回计数与覆盖率。"""
    root = Path(parsed_root or (FINKB_ROOT / "data" / "parsed"))
    aliases = _alias_index(Path(metric_dict_path) if metric_dict_path else None)
    store.conn.execute("BEGIN")
    store.conn.execute("DELETE FROM note_anchors")

    # 1) 报表页锚：facts 自带 source_doc/page_num。
    docs = [p for p in sorted(root.iterdir()) if p.is_dir() and re.fullmatch(r"\d{6}_", p.name[:7])]
    sections_by_doc = {doc.name: _page_sections(doc) for doc in docs}
    statement_rows = store.conn.execute(
        "SELECT DISTINCT company, period, metric_id, source_doc, page_num FROM facts"
    ).fetchall()
    doc_period = {str(source_doc): str(period) for _c, period, _m, source_doc, _p in statement_rows}
    rows = []
    for company, period, metric_id, source_doc, page_num in statement_rows:
        section = sections_by_doc.get(str(source_doc), {}).get(int(page_num or 0), "")
        rows.append(
            (str(source_doc), str(company), str(period), str(metric_id),
             "statement", int(page_num or 0), section, section, "facts")
        )

    # 2) 附注锚：章节标题里出现指标别名。
    note_rows = []
    for doc in docs:
        for page, section, _text in iter_sections(doc):
            title = norm_label(section)
            if not title:
                continue
            for metric_id, alias in aliases:
                if alias and alias in title:
                    note_rows.append(
                        (doc.name, doc.name.split("_", 1)[0], doc_period.get(doc.name, ""),
                         metric_id, "note", page, section, section, "section_match")
                    )
    # 同页同章节只留一条（长别名优先已保证 metric 不重复）。
    seen = set()
    deduped = []
    for row in note_rows:
        key = (row[0], row[3], row[4], row[5], row[6])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    rows.extend(deduped)
    if rows:
        store.conn.executemany(
            "INSERT INTO note_anchors "
            "(doc_id, company, period, metric_id, anchor_type, page_num, section, title, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    store.conn.execute("COMMIT")
    return {"statement": len(statement_rows), "note": len(deduped), "total": len(rows)}


def coverage(store) -> dict:
    """按指标统计报表页锚/附注锚覆盖的文档数，供验收与 DEVLOG 引用。"""
    rows = store.conn.execute(
        "SELECT metric_id, anchor_type, count(DISTINCT doc_id) "
        "FROM note_anchors GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    report: dict[str, dict] = {}
    for metric_id, anchor_type, docs in rows:
        report.setdefault(str(metric_id), {})[str(anchor_type)] = int(docs)
    statement_docs = sum(1 for item in report.values() if item.get("statement", 0) > 0)
    note_docs = sum(1 for item in report.values() if item.get("note", 0) > 0)
    return {
        "metrics": len(report),
        "with_statement": statement_docs,
        "with_note": note_docs,
        "detail": report,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成附注锚点")
    parser.add_argument("--db", default=str(FINKB_ROOT / "data" / "facts.duckdb"))
    parser.add_argument("--parsed", default=str(FINKB_ROOT / "data" / "parsed"))
    parser.add_argument("--metric-dict", default=None)
    args = parser.parse_args()

    from finkb.fact_engine.store import FactStore

    with FactStore(Path(args.db)) as store:
        print(json.dumps(build(store, args.parsed, args.metric_dict), ensure_ascii=False))
        print(json.dumps(coverage(store), ensure_ascii=False)[:800])


if __name__ == "__main__":
    main()
