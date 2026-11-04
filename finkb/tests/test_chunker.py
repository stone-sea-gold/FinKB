"""块级切块（search/chunker.py）单测。"""

from __future__ import annotations

import json
from pathlib import Path

from finkb.search import chunker


def _doc(root: Path, name="000001_2025_年报"):
    doc = root / name
    doc.mkdir(parents=True)
    long_table = "\n".join(
        ["| 项目 | 金额 | 说明 |", "|---|---|---|"]
        + [f"| 科目{i} | {i * 100} | 说明文字{i} |" for i in range(120)]
    )
    lines = [
        '<!-- META: {"doc": "000001_2025_年报", "page": 1, "section": "第三节 管理层讨论与分析"} -->',
        "公司主要业务是制造与销售。",
        "",
        "报告期内经营情况稳定。",
        '<!-- META: {"doc": "000001_2025_年报", "page": 2, "section": "分产品情况"} -->',
        long_table,
        '<!-- META: {"doc": "000001_2025_年报", "page": 3, "section": "续页"} -->',
        "",
    ]
    (doc / "page_anchored.md").write_text("\n".join(lines), encoding="utf-8")
    return doc


def test_chunk_document_merges_text_and_sets_section(tmp_path):
    doc = _doc(tmp_path)
    chunks = chunker.chunk_document(doc, {"company": "000001", "period": "2025"})
    text_chunks = [c for c in chunks if c["block_type"] == "text"]
    assert text_chunks
    assert text_chunks[0]["page_num"] == 1
    assert text_chunks[0]["section"] == "第三节 管理层讨论与分析"
    assert "主要业务" in text_chunks[0]["text"]
    assert "经营情况稳定" in text_chunks[0]["text"]  # 同页正文被合并
    assert text_chunks[0]["company"] == "000001"
    assert text_chunks[0]["period"] == "2025"


def test_table_chunks_repeat_header(tmp_path):
    doc = _doc(tmp_path)
    chunks = chunker.chunk_document(doc, {})
    tables = [c for c in chunks if c["block_type"] == "table"]
    assert len(tables) > 1
    assert all(c["text"].splitlines()[0] == "| 项目 | 金额 | 说明 |" for c in tables)
    assert all(c["table_header"] == "| 项目 | 金额 | 说明 |" for c in tables)


def test_empty_continuation_page_is_skipped(tmp_path):
    doc = _doc(tmp_path)
    chunks = chunker.chunk_document(doc, {})
    assert all(c["page_num"] != 3 for c in chunks)


def test_chunk_ids_are_unique_and_stable(tmp_path):
    doc = _doc(tmp_path)
    first = chunker.chunk_document(doc, {})
    second = chunker.chunk_document(doc, {})
    assert [c["chunk_id"] for c in first] == [c["chunk_id"] for c in second]
    assert len({c["chunk_id"] for c in first}) == len(first)


def test_build_and_verify(tmp_path):
    root = tmp_path / "parsed"
    _doc(root)
    out = tmp_path / "chunks.jsonl"
    stats = chunker.build(root, out)
    assert stats["docs"] == 1
    assert stats["chunks"] == stats["text"] + stats["table"]
    report = chunker.verify(root, out)
    assert report["checked"] == stats["chunks"]
    assert report["missing_pages"] == 0
    assert report["text_mismatch"] == 0
    assert report["table_mismatch"] == 0


def test_verify_detects_bad_chunk(tmp_path):
    root = tmp_path / "parsed"
    _doc(root)
    out = tmp_path / "chunks.jsonl"
    chunker.build(root, out)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    rows[0]["text"] = "这段文字不在原文里"
    out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    report = chunker.verify(root, out)
    assert report["text_mismatch"] >= 1
