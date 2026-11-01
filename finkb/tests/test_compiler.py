"""单文档编译器的单测（用假 LLM，不联网）。"""

from __future__ import annotations

import json

from finkb.fact_engine.store import FactStore
from finkb.wiki import compiler
from finkb.wiki import vault


class FakeClient:
    def __init__(self, body: str, title: str = "示例页") -> None:
        self.body = body
        self.title = title
        self.calls = 0

    def chat_json(self, system: str, user: str) -> dict:
        self.calls += 1
        return {"title": self.title, "description": "一句话摘要", "body_markdown": self.body}


def _store() -> FactStore:
    store = FactStore(":memory:")
    store.insert_fact(
        company="000001",
        period="2025",
        metric_id="revenue",
        unit_family="money",
        unit_of_account="元",
        norm_unit="亿元",
        value_raw=10000000000.0,
        raw_text_matched="10,000,000,000.00",
        value_norm=100.0,
        caliber="合并报表",
        reporting_party="000001",
        report_date="2026-03-01",
        source_doc="000001_2025_年报",
        page_num=1,
        table_id="P1_T1",
        match_level="exact",
    )
    return store


def _one_page(monkeypatch):
    monkeypatch.setattr(
        compiler,
        "_page_specs",
        lambda doc_id, company, period: [
            {
                "page_type": "summary",
                "slug": doc_id,
                "title": "摘要",
                "catalog": ("revenue",),
                "segments": False,
                "cash": False,
                "brief": "写摘要",
            }
        ],
    )


def _run(tmp_path, monkeypatch, body: str):
    _one_page(monkeypatch)
    data_root = tmp_path / "data"
    doc = data_root / "parsed" / "000001_2025_年报"
    doc.mkdir(parents=True)
    (doc / "report.json").write_text(json.dumps({"pages": 3}), encoding="utf-8")
    (doc / "page_anchored.md").write_text('<!-- META: {"doc": "000001_2025_年报", "page": 1} -->\n正文', encoding="utf-8")
    client = FakeClient(body)
    with _store() as store:
        return compiler.compile_document(
            client,
            store,
            "000001_2025_年报",
            company="000001",
            period="2025",
            vault_root=tmp_path / "vault",
            claims_db=tmp_path / "wiki.duckdb",
            data_root=data_root,
        )


def test_compile_active_page(tmp_path, monkeypatch):
    results = _run(tmp_path, monkeypatch, "营业收入 {{c1}} 亿元。")
    assert results[0]["status"] == "active"
    page = vault.read_page(tmp_path / "vault" / "summaries" / "000001_2025_年报.md")
    assert page.meta["status"] == "active"
    assert "100.00[^c1]" in page.body
    assert any(s.startswith("l1:facts#") for s in page.meta["sources"])


def test_unknown_placeholder_stays_draft(tmp_path, monkeypatch):
    results = _run(tmp_path, monkeypatch, "营业收入 {{c99}} 亿元。")
    assert results[0]["status"] == "draft"
    assert "未知占位符" in results[0]["reason"]


def test_direct_number_blocks_promotion(tmp_path, monkeypatch):
    results = _run(tmp_path, monkeypatch, "营业收入 123 亿元，见 {{c1}} 亿元。")
    assert results[0]["status"] == "draft"
    assert any("数字与 claim 不符" in msg or "没有 claim 覆盖" in msg for msg in results[0]["issues"])
    assert (tmp_path / "vault" / "_draft" / "summaries" / "000001_2025_年报.md").is_file()


def test_single_layer_source_stays_draft(tmp_path, monkeypatch):
    """只有 l0 一层来源时不得转正（ADR-041 的门禁在编译流程里生效）。"""
    monkeypatch.setattr(compiler.frontmatter, "has_enough_sources", lambda meta: False)
    results = _run(tmp_path, monkeypatch, "营业收入 {{c1}} 亿元。")
    assert results[0]["status"] == "draft"
    assert "来源不足两层" in results[0]["reason"]
    assert not (tmp_path / "vault" / "summaries" / "000001_2025_年报.md").exists()
