"""附注锚点（notes.py + note_drill_down）单测。"""

from __future__ import annotations

import json

import yaml

from finkb.fact_engine import notes
from finkb.fact_engine.store import FactStore
from finkb.ontology.operators import note_drill_down


def _metric_yaml(tmp_path):
    path = tmp_path / "metric_dict.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "metrics": [
                    {"metric_id": "short_term_borrowings", "metric_name": "短期借款", "aliases": ["短期借款"]},
                    {"metric_id": "revenue", "metric_name": "营业总收入", "aliases": ["营业收入"]},
                    {"metric_id": "cash", "metric_name": "货币资金", "aliases": ["现金"]},
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def _doc(tmp_path):
    doc = tmp_path / "parsed" / "000001_2025_年报"
    doc.mkdir(parents=True)
    lines = [
        '<!-- META: {"doc": "000001_2025_年报", "page": 1, "section": "合并资产负债表"} -->',
        "货币资金 100",
        '<!-- META: {"doc": "000001_2025_年报", "page": 20, "section": "24、短期借款"} -->',
        "短期借款明细如下",
        '<!-- META: {"doc": "000001_2025_年报", "page": 30, "section": "45、营业收入和营业成本"} -->',
        "营业收入明细如下",
    ]
    (doc / "page_anchored.md").write_text("\n".join(lines), encoding="utf-8")
    return tmp_path / "parsed"


def _fact(store, metric, page):
    return store.insert_fact(
        company="000001",
        period="2025",
        metric_id=metric,
        unit_family="monetary",
        unit_of_account="元",
        norm_unit="元",
        value_raw=100.0,
        raw_text_matched="100",
        value_norm=100.0,
        caliber="合并报表",
        reporting_party="000001",
        report_date="2026-03-01",
        source_doc="000001_2025_年报",
        page_num=page,
        table_id="P1_T1",
        match_level="exact",
    )


def test_iter_sections(tmp_path):
    root = _doc(tmp_path)
    sections = list(notes.iter_sections(root / "000001_2025_年报"))
    assert [(p, s) for p, s, _t in sections] == [
        (1, "合并资产负债表"),
        (20, "24、短期借款"),
        (30, "45、营业收入和营业成本"),
    ]
    assert "短期借款明细" in sections[1][2]


def test_alias_index_filters_generic(tmp_path):
    pairs = notes._alias_index(_metric_yaml(tmp_path))
    aliases = {alias for _m, alias in pairs}
    assert "短期借款" in aliases
    assert "现金" not in aliases  # 泛词被过滤


def test_build_statement_and_note_anchors(tmp_path):
    root = _doc(tmp_path)
    with FactStore(":memory:") as store:
        _fact(store, "short_term_borrowings", 1)
        _fact(store, "revenue", 30)
        counts = notes.build(store, root, _metric_yaml(tmp_path))
        rows = store.conn.execute(
            "SELECT metric_id, anchor_type, page_num FROM note_anchors ORDER BY 2, 1"
        ).fetchall()
    assert counts["statement"] == 2
    assert counts["note"] == 2
    assert ("short_term_borrowings", "note", 20) in rows
    assert ("revenue", "note", 30) in rows


def test_coverage(tmp_path):
    root = _doc(tmp_path)
    with FactStore(":memory:") as store:
        _fact(store, "short_term_borrowings", 1)
        _fact(store, "revenue", 30)
        notes.build(store, root, _metric_yaml(tmp_path))
        report = notes.coverage(store)
    assert report["metrics"] == 2
    assert report["with_statement"] == 2
    assert report["with_note"] == 2


def test_note_drill_down(tmp_path):
    root = _doc(tmp_path)
    with FactStore(":memory:") as store:
        _fact(store, "short_term_borrowings", 1)
        notes.build(store, root, _metric_yaml(tmp_path))
        result = note_drill_down(store.conn, "000001", "2025", "short_term_borrowings")
    assert result["missing_note"] is False
    assert result["statement"][0]["page_num"] == 1
    assert result["notes"][0]["page_num"] == 20
    assert result["notes"][0]["section"] == "24、短期借款"


def test_note_drill_down_falls_back_to_latest_period(tmp_path):
    root = _doc(tmp_path)
    with FactStore(":memory:") as store:
        _fact(store, "short_term_borrowings", 1)
        notes.build(store, root, _metric_yaml(tmp_path))
        result = note_drill_down(store.conn, "000001", "2024", "short_term_borrowings")
    assert result["statement"] == []
    assert result["notes"] and result["notes"][0]["page_num"] == 20
    assert result["missing_note"] is False
