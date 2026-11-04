"""实体主数据与消歧（entity_registry）单测。"""

from __future__ import annotations

import yaml

from finkb.fact_engine import entity_registry
from finkb.fact_engine.store import FactStore


def _scope(tmp_path):
    path = tmp_path / "wiki_scope.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "docs": [
                    {"doc_id": "000001_2025_年报", "company": "000001", "company_name": "平安银行", "period": "2025"},
                    {"doc_id": "000002_2025_年报", "company": "000002", "company_name": "万科企业", "period": "2025"},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def _edge(store, dst, dst_type="entity", src="000001", doc="000001_2025_年报"):
    store.conn.execute(
        "INSERT INTO relations (src_type, src_id, rel_type, dst_type, dst_id, source_doc, page_num, attrs_json, period) "
        "VALUES ('company', ?, 'subsidiary_of', ?, ?, ?, 1, '{}', '2025')",
        [src, dst_type, dst, doc],
    )


def test_rebuild_companies_and_aliases(tmp_path):
    with FactStore(":memory:") as store:
        counts = entity_registry.rebuild(store, _scope(tmp_path))
        rows = store.conn.execute("SELECT entity_id, canonical_name FROM entities_master ORDER BY 1").fetchall()
        aliases = {a for (a,) in store.conn.execute("SELECT alias FROM entity_aliases").fetchall()}
    assert counts["entities"] == 2
    assert ("co:000001", "平安银行") in rows
    assert {"000001", "平安银行"} <= aliases


def test_rebuild_merges_suffix_variants(tmp_path):
    with FactStore(":memory:") as store:
        _edge(store, "某某有限公司")
        _edge(store, "某某")
        counts = entity_registry.rebuild(store, _scope(tmp_path))
        entity_registry.annotate(store)
        ids = [r[0] for r in store.conn.execute(
            "SELECT DISTINCT dst_entity_id FROM relations WHERE dst_entity_id IS NOT NULL"
        ).fetchall()]
    assert counts["by_type"]["organization"] == 1
    assert len(ids) == 1


def test_person_entity_type(tmp_path):
    with FactStore(":memory:") as store:
        _edge(store, "张三", dst_type="person")
        counts = entity_registry.rebuild(store, _scope(tmp_path))
        row = store.conn.execute(
            "SELECT entity_type FROM entities_master WHERE canonical_name = '张三'"
        ).fetchone()
    assert counts["by_type"]["person"] == 1
    assert row == ("person",)


def test_gate_blocks_noise_rows(tmp_path):
    with FactStore(":memory:") as store:
        _edge(store, "合计")
        _edge(store, "占净资产比例")
        counts = entity_registry.rebuild(store, _scope(tmp_path))
    assert counts["entities"] == 2  # 只有两家上市公司，噪音行被挡掉


def test_annotate_resolves_edges(tmp_path):
    with FactStore(":memory:") as store:
        _edge(store, "某某有限公司")
        entity_registry.rebuild(store, _scope(tmp_path))
        result = entity_registry.annotate(store)
        rows = store.conn.execute(
            "SELECT src_entity_id, dst_entity_id FROM relations"
        ).fetchall()
    assert result["unresolved"] == 0
    assert result["resolved"] == 1
    assert rows[0][0] == "co:000001"
    assert rows[0][1].startswith("org:")


def test_rebuild_idempotent(tmp_path):
    with FactStore(":memory:") as store:
        _edge(store, "某某有限公司")
        first = entity_registry.rebuild(store, _scope(tmp_path))
        second = entity_registry.rebuild(store, _scope(tmp_path))
        count = store.conn.execute("SELECT count(*) FROM entity_aliases").fetchone()[0]
    assert first == second
    assert count == first["aliases"]
