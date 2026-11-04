"""从结构化表格抽实体关系（ADR-046 / ADR-059）。

四类有稳定表头的关系：

- ``subsidiary_of``：主要子公司表（「子公司名称 / 子公司全称」+ 持股比例）
- ``related_party_of``：关联方表（「关联方」+ 关系 / 交易金额）
- ``shareholder_of``：前十名股东表（「股东名称」+ 持股比例 / 股东性质）
- ``guarantees_for``：重大担保表（「担保对象名称 / 被担保方」+ 担保金额）

表头族写在 ``config/relation_headers.yaml``，代码只按序匹配、按列取值；
行标签先过 :mod:`finkb.fact_engine.entity_gate` 的门禁。每条边都带
``source_doc + page_num + period``，属性（比例/金额/关系）写进 ``attrs_json``；
``src_entity_id`` / ``dst_entity_id`` 由 :mod:`finkb.fact_engine.entity_registry`
在全部文档写完后统一消歧回填。
"""

from __future__ import annotations

import argparse
import json
import re
from functools import lru_cache
from pathlib import Path

import yaml

from finkb.fact_engine import entity_gate
from finkb.fact_engine.selector import FINKB_ROOT
from finkb.fact_engine.selector import SourceTable, load_manifest, load_tables, norm_label

PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
HEADER_PATH = FINKB_ROOT / "config" / "relation_headers.yaml"


@lru_cache(maxsize=4)
def _load_headers(path: str) -> dict:
    file = Path(path)
    if not file.exists():
        return {"families": []}
    return yaml.safe_load(file.read_text(encoding="utf-8")) or {"families": []}


def load_header_config(path: str | Path | None = None) -> dict:
    return _load_headers(str(path or HEADER_PATH))


def _cells(table: SourceTable) -> list[str]:
    return [norm_label(cell) for cell in table.header]


def _find_col(cells: list[str], tokens: list[str] | None) -> int | None:
    """返回第一个包含任一关键词的列下标。"""
    for index, cell in enumerate(cells):
        for token in tokens or []:
            if token and token in cell:
                return index
    return None


def classify(table: SourceTable, cfg: dict | None = None) -> tuple[str, dict] | None:
    """按表头族判断关系类型，返回 ``(rel_type, family_spec)``。"""
    cfg = cfg or load_header_config()
    cells = _cells(table)
    if cells:
        joined = "|".join(cells)
        for family in cfg.get("families") or []:
            if not any(token in joined for token in family.get("match_any") or []):
                continue
            if any(token in joined for token in family.get("exclude_any") or []):
                continue
            return family["rel_type"], family
    # 前十名股东表常常没有表头（表头被拆到上一块），靠章节 + 行形状兜底：
    # 第二列是股东类型（境内自然人/境外法人…），第三列是百分比。
    if not cells and "股东" in norm_label(table.section):
        return "shareholder_of", {
            "name_any": ["股东名称"],
            "legacy": True,
            "attrs": {
                "ratio": {"col": 2, "type": "percent"},
                "holder_type": {"col": 1, "type": "text"},
            },
        }
    return None


def _ratio(value: object) -> float | None:
    text = str(value or "").strip()
    match = PERCENT.search(text)
    if match:
        return float(match.group(1))
    # 持股比例列常写成「80.00」不带百分号，按百分数理解。
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _attr_value(raw: object, rule: dict) -> object | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if rule.get("type") == "percent":
        return _ratio(text)
    return text


def parse_table(table: SourceTable, kind: str, spec: dict, company: str) -> list[dict]:
    """把一张关系表解析成边列表；名字列/属性列都按表头族定位。"""
    cells = _cells(table)
    legacy = bool(spec.get("legacy"))
    name_col = 0 if legacy else (_find_col(cells, spec.get("name_any")) or 0)
    attr_cols: dict[str, tuple[int, dict]] = {}
    for attr, rule in (spec.get("attrs") or {}).items():
        if "col" in rule:
            attr_cols[attr] = (int(rule["col"]), rule)
            continue
        col = _find_col(cells, rule.get("any"))
        if col is not None:
            attr_cols[attr] = (col, rule)

    edges: list[dict] = []
    for row in table.rows:
        if len(row) <= name_col:
            continue
        raw_name = str(row[name_col] or "").strip()
        if entity_gate.gate_reason(raw_name) is not None:
            continue
        name = entity_gate.normalize(raw_name)
        attrs: dict = {}
        for attr, (col, rule) in attr_cols.items():
            if col >= len(row):
                continue
            value = _attr_value(row[col], rule)
            if value is not None:
                attrs[attr] = value
        dst_type = "entity"
        if "自然人" in str(attrs.get("holder_type") or ""):
            dst_type = "person"
        elif kind == "shareholder_of" and any("自然人" in str(cell) for cell in row):
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
                "period": table.period,
                "attrs": attrs,
            }
        )
    return edges


def _iter_tables(tables: list[SourceTable]):
    """逻辑子表各自有表头，逐张判型；没有子表时退回父表。"""
    for parent in tables:
        if parent.children:
            yield from parent.children
        else:
            yield parent


def write_relations(store, doc_dir: Path, manifest: dict) -> int:
    """解析一份文档并覆盖写它的关系边，返回条数。"""
    if not (doc_dir / "report.json").exists():
        return 0
    tables = load_tables(doc_dir, manifest)
    doc_id = tables[0].doc_id if tables else doc_dir.name
    company = tables[0].company if tables else ""
    store.conn.execute("BEGIN")
    store.conn.execute("DELETE FROM relations WHERE source_doc = ?", [doc_id])
    written = 0
    seen: set[tuple] = set()
    cfg = load_header_config()
    for table in _iter_tables(tables):
        matched = classify(table, cfg)
        if matched is None:
            continue
        kind, spec = matched
        for edge in parse_table(table, kind, spec, company):
            attrs_json = json.dumps(edge["attrs"], ensure_ascii=False, sort_keys=True)
            key = (
                edge["src_id"],
                edge["rel_type"],
                edge["dst_id"],
                edge["source_doc"],
                edge["page_num"],
                edge["period"],
                attrs_json,
            )
            if key in seen:
                continue
            seen.add(key)
            store.conn.execute(
                "INSERT INTO relations "
                "(src_type, src_id, rel_type, dst_type, dst_id, source_doc, page_num, "
                " attrs_json, period) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    edge["src_type"],
                    edge["src_id"],
                    edge["rel_type"],
                    edge["dst_type"],
                    edge["dst_id"],
                    edge["source_doc"],
                    edge["page_num"],
                    attrs_json,
                    edge["period"],
                ],
            )
            written += 1
    store.conn.execute("COMMIT")
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
