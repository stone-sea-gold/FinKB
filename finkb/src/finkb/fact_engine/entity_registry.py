"""实体主数据与消歧（ADR-059，S2）。

关系边里的人名/机构名先过门禁，再在这里合成主数据：

- 上市公司：``co:<6 位代码>``，来自 ``config/wiki_scope.yaml``，别名含代码、简称；
- 其他机构：``org:<hash>``；自然人：``person:<hash>``，按归一化名字去重。

别名表 ``entity_aliases`` 负责把「XX有限公司 / XX / 全称带括号」等写法并到同一
实体；``relations.dst_entity_id`` 在全部文档写完后统一回填，原始 ``dst_id``
保留不动，方便回查原文。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

from finkb.fact_engine import entity_gate
from finkb.fact_engine.selector import FINKB_ROOT

SCOPE_PATH = FINKB_ROOT / "config" / "wiki_scope.yaml"
ENTITY_TYPES = ("listed_company", "organization", "person")


def _hash_id(prefix: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def _load_scope(path: str | Path | None = None) -> dict:
    file = Path(path or SCOPE_PATH)
    return yaml.safe_load(file.read_text(encoding="utf-8")) or {"docs": []}


def _company_entities(scope: dict) -> tuple[dict[str, dict], dict[str, str]]:
    """从 wiki_scope 生成上市公司实体与「别名 → 实体」索引。"""
    entities: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    for doc in scope.get("docs") or []:
        code = str(doc.get("company") or "").strip()
        if not code:
            continue
        entity_id = f"co:{code}"
        name = str(doc.get("company_name") or code)
        entity = entities.setdefault(
            entity_id,
            {"canonical_name": name, "entity_type": "listed_company", "aliases": set(), "source": "wiki_scope"},
        )
        entity["aliases"].update(entity_gate.match_keys(name))
        entity["aliases"].add(code)
    for entity_id, entity in entities.items():
        for alias in entity["aliases"]:
            aliases.setdefault(alias, entity_id)
    return entities, aliases


def rebuild(store, scope_path: str | Path | None = None) -> dict:
    """重建 entities_master / entity_aliases；返回计数。"""
    scope = _load_scope(scope_path)
    entities, company_alias = _company_entities(scope)

    def add(entity_id: str, name: str, entity_type: str, aliases: list[str], source: str) -> None:
        entity = entities.setdefault(
            entity_id,
            {"canonical_name": name, "entity_type": entity_type, "aliases": set(), "source": source},
        )
        entity["aliases"].update(aliases)

    rows = store.conn.execute(
        "SELECT DISTINCT src_type, src_id, dst_type, dst_id FROM relations"
    ).fetchall()
    listed = {e.split(":")[1] for e in entities if e.startswith("co:")}

    def resolve(raw: str, value_type: str) -> None:
        """把关系边一端（源或目标）登记成实体。源也可能是非上市子公司/自然人。"""
        raw = str(raw or "")
        if entity_gate.gate_reason(raw) is not None:
            return
        keys = entity_gate.match_keys(raw)
        if not keys:
            return
        matched = next((company_alias[key] for key in keys if key in company_alias), None)
        if matched:
            add(matched, entities[matched]["canonical_name"], "listed_company", keys, "relations")
            return
        etype = "person" if value_type == "person" else "organization"
        prefix = "person" if etype == "person" else "org"
        # 用「去后缀后的最短写法」做稳定主键：XX有限公司 与 XX 会并到同一实体。
        canonical = keys[-1]
        add(_hash_id(prefix, canonical), canonical, etype, keys, "relations")

    for src_type, src_id, dst_type, dst_id in rows:
        if src_type == "company" and str(src_id) in listed:
            add(f"co:{src_id}", str(src_id), "listed_company", [str(src_id)], "relations")
        else:
            resolve(str(src_id), src_type)
        resolve(str(dst_id), dst_type)

    store.conn.execute("BEGIN")
    store.conn.execute("DELETE FROM entities_master")
    store.conn.execute("DELETE FROM entity_aliases")
    master_rows = []
    alias_rows = []
    for entity_id, entity in sorted(entities.items()):
        alias_list = sorted(entity["aliases"])
        master_rows.append(
            (
                entity_id,
                entity["canonical_name"],
                entity["entity_type"],
                json.dumps(alias_list, ensure_ascii=False),
                entity["source"],
            )
        )
        for alias in alias_list:
            alias_rows.append((alias, entity_id, "name"))
    if master_rows:
        store.conn.executemany(
            "INSERT INTO entities_master VALUES (?, ?, ?, ?, ?)", master_rows
        )
    if alias_rows:
        store.conn.executemany(
            "INSERT INTO entity_aliases VALUES (?, ?, ?)", alias_rows
        )
    store.conn.execute("COMMIT")
    return {
        "entities": len(master_rows),
        "aliases": len(alias_rows),
        "by_type": {
            etype: sum(1 for e in entities.values() if e["entity_type"] == etype)
            for etype in ENTITY_TYPES
        },
    }


def annotate(store) -> dict:
    """把 relations 的 src/dst 名字解析成 entity_id；返回解析统计。"""
    alias_rows = store.conn.execute("SELECT alias, entity_id FROM entity_aliases").fetchall()
    alias_map = {str(alias): str(entity_id) for alias, entity_id in alias_rows}
    company_ids = {
        str(entity_id).split(":", 1)[1]
        for (entity_id,) in store.conn.execute(
            "SELECT entity_id FROM entities_master WHERE entity_type = 'listed_company'"
        ).fetchall()
    }
    rows = store.conn.execute(
        "SELECT src_id, dst_id, source_doc, page_num, rel_type FROM relations"
    ).fetchall()
    resolved = unresolved = 0
    updates = []
    for src_id, dst_id, source_doc, page_num, rel_type in rows:
        src_entity = f"co:{src_id}" if str(src_id) in company_ids else None
        if src_entity is None:
            src_entity = next(
                (alias_map[key] for key in entity_gate.match_keys(src_id) if key in alias_map), None
            )
        keys = entity_gate.match_keys(dst_id)
        dst_entity = next((alias_map[key] for key in keys if key in alias_map), None)
        if dst_entity:
            resolved += 1
        else:
            unresolved += 1
        updates.append((src_entity, dst_entity, source_doc, page_num, src_id, rel_type, dst_id))
    store.conn.execute("BEGIN")
    store.conn.executemany(
        "UPDATE relations SET src_entity_id = ?, dst_entity_id = ? "
        "WHERE source_doc = ? AND page_num = ? AND src_id = ? AND rel_type = ? AND dst_id = ?",
        updates,
    )
    store.conn.execute("COMMIT")
    return {"resolved": resolved, "unresolved": unresolved}


def main() -> None:
    parser = argparse.ArgumentParser(description="重建实体主数据并回填关系边")
    parser.add_argument("--db", default=str(FINKB_ROOT / "data" / "facts.duckdb"))
    parser.add_argument("--scope", default=str(SCOPE_PATH))
    parser.add_argument("--no-annotate", action="store_true")
    args = parser.parse_args()

    from finkb.fact_engine.store import FactStore

    with FactStore(Path(args.db)) as store:
        counts = rebuild(store, args.scope)
        print(json.dumps(counts, ensure_ascii=False))
        if not args.no_annotate:
            print(json.dumps(annotate(store), ensure_ascii=False))


if __name__ == "__main__":
    main()
