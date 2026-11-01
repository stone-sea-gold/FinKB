"""L2 wiki 骨架：frontmatter、原子写、draft 隔离、索引（ADR-041 / ADR-043）。"""

from __future__ import annotations

import pytest

from finkb.wiki import frontmatter, vault


def _meta(**overrides):
    meta = frontmatter.build(
        page_type="summary",
        title="比亚迪 2025 年报",
        description="一句话摘要",
        sources=["l0:002594_2025_年报", "l1:facts#002594"],
        company="002594",
        period="2025",
        tags=["汽车"],
    )
    meta.update(overrides)
    return meta


def test_frontmatter_roundtrip():
    meta = _meta()
    text = frontmatter.dump_frontmatter(meta) + "\n正文第一段。\n"
    parsed, body = frontmatter.split_frontmatter(text)
    assert parsed["type"] == "summary"
    assert parsed["sources"] == meta["sources"]
    assert body.strip() == "正文第一段。"


def test_split_frontmatter_rejects_missing_header():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.split_frontmatter("没有 frontmatter")


def test_validate_reports_missing_and_bad_values():
    problems = frontmatter.validate({"type": "unknown", "status": "done", "sources": ["bad"]})
    assert any("title" in p for p in problems)
    assert any("description" in p for p in problems)
    assert any("type 非法" in p for p in problems)
    assert any("status 非法" in p for p in problems)
    assert any("sources 条目格式非法" in p for p in problems)


def test_source_layers_follow_adr_041():
    assert not frontmatter.has_enough_sources({"sources": ["l0:doc"]})
    assert frontmatter.has_enough_sources({"sources": ["l0:doc", "l1:facts#000001"]})
    assert frontmatter.source_layers(["l1:facts#000001"]) == {"l1"}


def test_write_page_active_and_draft(tmp_path):
    root = vault.init_vault(tmp_path / "wiki_vault")
    active = vault.write_page(root, "summary", "doc_a", _meta(), "正文")
    assert active == root / "summaries" / "doc_a.md"
    assert vault.read_page(active).meta["status"] == "active"

    draft_meta = _meta(status="draft", sources=["l0:doc"])
    draft = vault.write_page(root, "summary", "doc_b", draft_meta, "正文")
    assert draft == root / "_draft" / "summaries" / "doc_b.md"


def test_write_page_rejects_dashes_in_body(tmp_path):
    root = vault.init_vault(tmp_path / "wiki_vault")
    with pytest.raises(frontmatter.FrontmatterError):
        vault.write_page(root, "summary", "doc_a", _meta(), "正文\n---\n第二段")


def test_atomic_write_leaves_no_temp_file(tmp_path):
    target = tmp_path / "a" / "b.md"
    vault.atomic_write(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert list(target.parent.glob(".*.tmp")) == []


def test_index_lists_active_only_and_is_deterministic(tmp_path):
    root = vault.init_vault(tmp_path / "wiki_vault")
    vault.write_page(root, "summary", "doc_a", _meta(title="甲"), "正文")
    vault.write_page(root, "concept", "topic", _meta(page_type="concept", title="乙"), "正文")
    vault.write_page(root, "summary", "doc_c", _meta(status="draft", sources=["l0:x"]), "正文")

    text = vault.rebuild_index(root)
    assert "[[summaries/doc_a|甲]]" in text
    assert "[[concepts/topic|乙]]" in text
    assert "doc_c" not in text
    assert text == vault.render_index(vault.iter_pages(root))


def test_load_scope_reads_registered_docs(tmp_path):
    path = tmp_path / "wiki_scope.yaml"
    path.write_text(
        "version: 1\ndocs:\n  - doc_id: 002594_2025_年报\n    company: '002594'\n",
        encoding="utf-8",
    )
    docs = vault.load_scope(path)
    assert docs[0]["doc_id"] == "002594_2025_年报"
    assert vault.registered_doc_ids(path) == ["002594_2025_年报"]
