"""自动分批解析（``--auto-split``）的离线测试。

MinerU 单文件 200 页硬上限用 ``page_ranges`` 绕过，但**每批的 ``page_idx`` 都从 0
重新编号**，直接拼会互相覆盖页且不报错（坑-16）。这里钉住三点：切分边界、
偏移只动 ``page_idx``、合并后页码从 1 连续到总页数。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from finkb.parser import mineru_client
from finkb.parser.mineru_client import (
    DEFAULT_PAGE_LIMIT,
    MineruClient,
    ParsedDocument,
    chunk_ranges,
    content_list_to_pages,
    merge_batches,
    offset_content_list,
)

DOC = "601127_2025_年报"


def test_default_page_limit_matches_mineru_cap() -> None:
    assert DEFAULT_PAGE_LIMIT == 200


def test_chunk_ranges_splits_at_the_limit() -> None:
    assert chunk_ranges(268, 200) == [(1, 200), (201, 268)]
    assert chunk_ranges(224, 200) == [(1, 200), (201, 224)]


def test_chunk_ranges_keeps_one_chunk_when_within_limit() -> None:
    assert chunk_ranges(200, 200) == [(1, 200)]
    assert chunk_ranges(1, 200) == [(1, 1)]


def test_chunk_ranges_is_empty_without_pages() -> None:
    assert chunk_ranges(0, 200) == []
    assert chunk_ranges(10, 0) == []


def test_offset_shifts_page_idx_without_touching_the_input() -> None:
    items = [{"type": "text", "text": "正文", "page_idx": 0}]

    shifted = offset_content_list(items, 200)

    assert shifted == [{"type": "text", "text": "正文", "page_idx": 200}]
    assert items[0]["page_idx"] == 0


def test_merge_batches_keeps_pages_distinct() -> None:
    merged = merge_batches(
        [(1, [{"type": "text", "text": "一", "page_idx": 0}]),
         (2, [{"type": "text", "text": "二", "page_idx": 0}])]
    )

    assert [page.number for page in content_list_to_pages(merged, "doc")] == [1, 2]


def test_parse_pdf_split_merges_batches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str | None] = []

    def fake_parse_pdf(self, pdf_path, doc_id, workdir, page_ranges=None):
        calls.append(page_ranges)
        start = int(str(page_ranges).split("-")[0])
        return ParsedDocument(
            doc_id=doc_id,
            pages=[],
            markdown="",
            content_list=[{"type": "text", "text": f"第{start}页", "page_idx": 0}],
        )

    monkeypatch.setattr(mineru_client, "pdf_page_count", lambda path: 268)
    monkeypatch.setattr(MineruClient, "parse_pdf", fake_parse_pdf)
    client = object.__new__(MineruClient)

    document = client.parse_pdf_split(tmp_path / "a.pdf", DOC, tmp_path, 200)

    assert calls == ["1-200", "201-268"]
    assert [page.number for page in document.pages] == [1, 201]
    assert len(document.content_list) == 2


def test_parse_pdf_split_uses_one_call_when_within_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str | None] = []

    def fake_parse_pdf(self, pdf_path, doc_id, workdir, page_ranges=None):
        calls.append(page_ranges)
        return ParsedDocument(
            doc_id=doc_id,
            pages=[],
            markdown="",
            content_list=[{"type": "text", "text": "正文", "page_idx": 0}],
        )

    monkeypatch.setattr(mineru_client, "pdf_page_count", lambda path: 120)
    monkeypatch.setattr(MineruClient, "parse_pdf", fake_parse_pdf)
    client = object.__new__(MineruClient)

    client.parse_pdf_split(tmp_path / "a.pdf", DOC, tmp_path, 200)

    assert calls == [None]
