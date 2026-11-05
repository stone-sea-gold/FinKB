"""混合检索（search/hybrid.py）单测，全部离线（FakeEmbedder）。"""

from __future__ import annotations

import json

import numpy as np

from finkb.search import hybrid
from finkb.search.embed import FakeEmbedder


def _chunks(tmp_path):
    rows = [
        {"chunk_id": "d1:p1:0", "doc_id": "d1", "company": "000001", "period": "2025",
         "page_num": 1, "section": "管理层讨论", "block_type": "text",
         "text": "公司存货较上年增加百分之二十，主要系原材料备货。", "table_header": None},
        {"chunk_id": "d1:p2:0", "doc_id": "d1", "company": "000001", "period": "2025",
         "page_num": 2, "section": "研发投入", "block_type": "text",
         "text": "报告期内研发投入合计十二亿元，同比增长百分之三十。", "table_header": None},
        {"chunk_id": "d2:p1:0", "doc_id": "d2", "company": "000002", "period": "2025",
         "page_num": 1, "section": "海外业务", "block_type": "text",
         "text": "海外收入占比提升，汇率波动对利润产生影响。", "table_header": None},
    ]
    path = tmp_path / "chunks.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return path


def _build(tmp_path, dense=True):
    chunks = _chunks(tmp_path)
    db = tmp_path / "chunks.db"
    vectors = tmp_path / "vectors.npy"
    stats = hybrid.build_index(chunks, db, vectors, FakeEmbedder(32) if dense else None)
    return chunks, db, vectors, stats


def test_tokenize_splits_chinese_words():
    tokens = hybrid.tokenize("存货增加")
    assert tokens and all(token.strip() for token in tokens)


def test_build_and_fts_search(tmp_path):
    chunks, db, _vectors, stats = _build(tmp_path, dense=False)
    assert stats["chunks"] == 3
    results = hybrid.search("存货", db_path=db, chunks_path=chunks, embedder=None)
    assert results and results[0]["chunk_id"] == "d1:p1:0"
    assert results[0]["channels"] == ["fts"]


def test_two_char_query_hits(tmp_path):
    chunks, db, _vectors, _stats = _build(tmp_path, dense=False)
    results = hybrid.search("研发", db_path=db, chunks_path=chunks, embedder=None)
    assert results and results[0]["chunk_id"] == "d1:p2:0"


def test_company_filter(tmp_path):
    chunks, db, _vectors, _stats = _build(tmp_path, dense=False)
    results = hybrid.search("收入", db_path=db, chunks_path=chunks, embedder=None,
                            filters={"company": "000002"})
    assert results and all(item["company"] == "000002" for item in results)


def test_rrf_merges_channels(tmp_path):
    chunks, db, vectors, _stats = _build(tmp_path, dense=True)
    results = hybrid.search("海外收入", db_path=db, chunks_path=chunks, vectors_path=vectors,
                            embedder=FakeEmbedder(32))
    assert results
    channels = {channel for item in results for channel in item["channels"]}
    assert "fts" in channels and "dense" in channels
    rrf_scores = [item["rrf"] for item in results]
    assert rrf_scores == sorted(rrf_scores, reverse=True)


def test_vectors_saved_with_dims(tmp_path):
    _chunks, _db, vectors, stats = _build(tmp_path, dense=True)
    assert stats["dims"] == 32
    assert np.load(vectors).shape == (3, 32)


def test_empty_query_returns_empty(tmp_path):
    chunks, db, _vectors, _stats = _build(tmp_path, dense=False)
    assert hybrid.search("", db_path=db, chunks_path=chunks, embedder=None) == []


def test_missing_db_still_searches(tmp_path):
    # 检索已改为直接在块集合上打分，FTS 索引库缺失也能工作（架构简化）。
    chunks = _chunks(tmp_path)
    results = hybrid.search("存货", db_path=tmp_path / "nope.db", chunks_path=chunks, embedder=None)
    assert results and results[0]["chunk_id"] == "d1:p1:0"
