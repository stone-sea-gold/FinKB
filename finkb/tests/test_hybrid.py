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


class _CountingEmbedder(FakeEmbedder):
    """记录每次实际送进模型的文本，用来证明增量没有重复算。"""

    def __init__(self, dims: int = 32) -> None:
        super().__init__(dims)
        self.texts: list[str] = []

    def embed(self, texts):
        self.texts.extend(texts)
        return super().embed(texts)


def test_incremental_reuses_vectors_for_unchanged_chunks(tmp_path):
    chunks = _chunks(tmp_path)
    db = tmp_path / "chunks.db"
    vectors = tmp_path / "vectors.npy"
    first = _CountingEmbedder()
    hybrid.build_index(chunks, db, vectors, first)
    assert len(first.texts) == 3

    second = _CountingEmbedder()
    stats = hybrid.build_index(chunks, db, vectors, second)
    assert second.texts == []
    assert stats["reused"] == 3


def test_incremental_embeds_only_new_chunk(tmp_path):
    chunks = _chunks(tmp_path)
    db = tmp_path / "chunks.db"
    vectors = tmp_path / "vectors.npy"
    hybrid.build_index(chunks, db, vectors, _CountingEmbedder())

    rows = [json.loads(line) for line in chunks.read_text(encoding="utf-8").splitlines() if line.strip()]
    new_text = "公司新增一条海外业务说明。"
    rows.append(
        {"chunk_id": "d3:p1:0", "doc_id": "d3", "company": "000003", "period": "2025",
         "page_num": 1, "section": "海外业务", "block_type": "text",
         "text": new_text, "table_header": None}
    )
    chunks.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    second = _CountingEmbedder()
    stats = hybrid.build_index(chunks, db, vectors, second)
    assert second.texts == [new_text]
    assert stats["reused"] == 3
    assert stats["chunks"] == 4
    saved = np.load(vectors)
    assert saved.shape[0] == 4
    assert np.allclose(saved[3], np.asarray(second.embed([new_text])[0], dtype=np.float32))


def test_incremental_falls_back_to_full_embed_without_manifest(tmp_path):
    chunks = _chunks(tmp_path)
    db = tmp_path / "chunks.db"
    vectors = tmp_path / "vectors.npy"
    hybrid.build_index(chunks, db, vectors, _CountingEmbedder())
    (tmp_path / "vectors_manifest.json").unlink()
    import shutil

    shutil.rmtree(tmp_path / "vectors_parts")

    second = _CountingEmbedder()
    hybrid.build_index(chunks, db, vectors, second)
    assert len(second.texts) == 3
