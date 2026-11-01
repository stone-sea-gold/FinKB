"""从结构化表格抽实体关系（ADR-046）。

只做四类有稳定表头的关系，不做客户/供应商名单（年报只披露前五名合计）：

- ``subsidiary_of``：主要子公司表（「子公司全称」+ 直接/间接持股比例）
- ``related_party_of``：关联方关系表（「公司名称」+「关联方关系」）
- ``shareholder_of``：前十名股东表（「股东名称」+ 持股比例）
- ``guarantees_for``：重大担保表（「担保对象名称」+ 实际担保金额）

每条边都带 ``source_doc + page_num``，便于回原文核对；属性（比例/金额/关系）
写进 ``attrs_json``。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from finkb.fact_engine.selector import FINKB_ROOT
from finkb.fact_engine.selector import SourceTable, load_manifest, load_tables, norm_label

PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
SKIP = ("合计", "小计", "不适用", "报告期", "上述", "注")


def _joined(header: list[str]) -> str:
    return "".join(norm_label(cell) for cell in header)


def classify(table: SourceTable) -> str | None:
    """按表头/章节判断这是哪类关系表；认不出返回 None。"""
    header = _joined(table.header)
    section = norm_label(table.section)
    if "子公司全称" in header:
        return "subsidiary_of"
    if "公司名称" in header and "关联方关系" in header:
        return "related_party_of"
    if "担保对象名称" in header:
        return "guarantees_for"
    if "股东名称" in header and "持股比例" in header:
        return "shareholder_of"
    # 前十名股东表常常没有表头（表头被拆到上一块），靠章节 + 行形状兜底：
    # 第二列是股东类型（境内自然人/境外法人…），第三列是百分比。
    if "股东" in section and table.header == []:
        return "shareholder_of"
    return None


def _skip(name: str) -> bool:
    text = norm_label(name)
    return not text or any(word in text for word in SKIP)


def _ratio(value: str) -> float | None:
    match = PERCENT.search(str(value or ""))
    return float(match.group(1)) if match else None


def parse_table(table: SourceTable, kind: str, company: str) -> list[dict]:
    """把一张关系表解析成边列表。"""
    edges: list[dict] = []
    for row in table.rows:
        if len(row) < 2:
            continue
        name = str(row[0] or "").strip()
        if _skip(name):
            continue
        attrs: dict = {}
        dst_type = "entity"
        if kind == "subsidiary_of":
            if len(row) < 7:
                continue
            attrs = {"direct": row[5], "indirect": row[6]}
        elif kind == "related_party_of":
            attrs = {"relationship": str(row[1] or "").strip()}
        elif kind == "guarantees_for":
            attrs = {"amount": str(row[4] if len(row) > 4 else "").strip()}
        elif kind == "shareholder_of":
            if len(row) < 3:
                continue
            ratio = _ratio(row[2])
            if ratio is None:
                continue
            attrs = {"ratio": ratio, "holder_type": str(row[1] or "").strip()}
            if "自然人" in str(row[1]):
                dst_type = "person"
        edges.append(
            {
                "src_type": "company",
                "src_id": company,
                "rel_type": kind,
                "dst_type": dst_type,
                "dst_id": name,
                "source_doc": table.doc_id,
                "page_num": table.page,
                "attrs": attrs,
            }
        )
    return edges


def write_relations(store, doc_dir: Path, manifest: dict) -> int:
    """解析一份文档并覆盖写它的关系边，返回条数。"""
    if not (doc_dir / "report.json").exists():
        return 0
    tables = load_tables(doc_dir, manifest)
    doc_id = tables[0].doc_id if tables else doc_dir.name
    company = tables[0].company if tables else ""
    store.conn.execute("DELETE FROM relations WHERE source_doc = ?", [doc_id])
    written = 0
    seen: set[tuple] = set()
    for table in tables:
        kind = classify(table)
        if kind is None:
            continue
        for edge in parse_table(table, kind, company):
            key = (
                edge["src_id"],
                edge["rel_type"],
                edge["dst_id"],
                edge["source_doc"],
                edge["page_num"],
                json.dumps(edge["attrs"], ensure_ascii=False, sort_keys=True),
            )
            if key in seen:
                continue
            seen.add(key)
            store.conn.execute(
                "INSERT INTO relations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    edge["src_type"],
                    edge["src_id"],
                    edge["rel_type"],
                    edge["dst_type"],
                    edge["dst_id"],
                    edge["source_doc"],
                    edge["page_num"],
                    json.dumps(edge["attrs"], ensure_ascii=False, sort_keys=True),
                ],
            )
            written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="从结构化表格抽实体关系")
    parser.add_argument("--db", default=str(FINKB_ROOT / "data" / "facts.duckdb"))
    parser.add_argument("--parsed", default=str(FINKB_ROOT / "data" / "parsed"))
    args = parser.parse_args()

    from finkb.fact_engine.store import FactStore

    manifest = load_manifest()
    total = 0
    with FactStore(Path(args.db)) as store:
        for doc_dir in sorted(Path(args.parsed).iterdir()):
            if not re.fullmatch(r"\d{6}_", doc_dir.name[:7]):
                continue
            count = write_relations(store, doc_dir, manifest)
            total += count
            print(f"{doc_dir.name}: {count}")
    print(f"relations: {total}")


if __name__ == "__main__":
    main()
