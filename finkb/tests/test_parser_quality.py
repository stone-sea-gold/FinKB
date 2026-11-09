"""解析质量体检（ADR-071）：空表分类/chart/纯图片页/未覆盖页。

纯离线；用合成 content_list，不读开发语料。
"""

from __future__ import annotations

from types import SimpleNamespace

from finkb.parser.quality import analyze_quality, quality_warnings


def test_analyze_quality_classifies_placeholder_vs_suspicious():
    content_list = [
        {"type": "text", "page_idx": 0, "text": "正文"},
        {"type": "table", "page_idx": 0, "table_body": "<table><tr><td>1</td></tr></table>"},
        # 第 2 页的空块：被第 1 页开始的跨页长表覆盖 → 续页占位
        {"type": "table", "page_idx": 1, "table_body": ""},
        # 第 4 页的空块：没有长表覆盖 → 可疑
        {"type": "table", "page_idx": 3, "table_body": ""},
    ]
    quality = analyze_quality(
        content_list,
        pdf_pages=5,
        tables=[{"page_start": 1, "page_end": 3}],
    )
    assert quality["empty_table_count"] == 2
    assert quality["placeholder_table_count"] == 1
    assert quality["suspicious_empty_table_count"] == 1
    assert quality["empty_tables"][0]["placeholder"] is True
    assert quality["empty_tables"][0]["page"] == 2
    assert quality["suspicious_empty_tables"][0]["page"] == 4


def test_analyze_quality_without_table_spans_treats_empty_as_suspicious():
    quality = analyze_quality([{"type": "table", "page_idx": 1, "table_body": ""}])
    assert quality["placeholder_table_count"] == 0
    assert quality["suspicious_empty_table_count"] == 1


def test_analyze_quality_accepts_table_objects():
    table = SimpleNamespace(page_start=1, page_end=2)
    quality = analyze_quality(
        [{"type": "table", "page_idx": 1, "table_body": ""}],
        tables=[table],
    )
    assert quality["placeholder_table_count"] == 1
    assert quality["suspicious_empty_table_count"] == 0


def test_analyze_quality_detects_charts_and_image_pages():
    content_list = [
        {"type": "text", "page_idx": 0, "text": "正文"},
        {"type": "chart", "page_idx": 2},
        {"type": "image", "page_idx": 3},
        {"type": "header", "page_idx": 3, "text": "页眉"},
    ]
    quality = analyze_quality(content_list, pdf_pages=6)
    assert quality["chart_pages"] == [3]
    assert quality["chart_blocks"] == 1
    assert quality["image_only_pages"] == [4]
    assert quality["no_content_pages"] == [4]
    assert quality["missing_pages"] == [2, 5, 6]


def test_analyze_quality_clean_document_has_no_warnings():
    quality = analyze_quality(
        [
            {"type": "text", "page_idx": 0, "text": "正文"},
            {"type": "table", "page_idx": 1, "table_body": "<table></table>"},
        ],
        pdf_pages=2,
    )
    assert quality["empty_table_count"] == 0
    assert quality["chart_pages"] == []
    assert quality["image_only_pages"] == []
    assert quality["missing_pages"] == []
    assert quality_warnings(quality) == []


def test_placeholder_alone_does_not_warn():
    quality = analyze_quality(
        [{"type": "table", "page_idx": 1, "table_body": ""}],
        tables=[{"page_start": 1, "page_end": 2}],
    )
    assert quality_warnings(quality) == []


def test_quality_warnings_mention_each_problem_type():
    quality = analyze_quality(
        [
            {"type": "table", "page_idx": 0, "table_body": ""},
            {"type": "chart", "page_idx": 1},
            {"type": "image", "page_idx": 2},
        ],
        pdf_pages=5,
    )
    messages = "\n".join(quality_warnings(quality))
    assert "可疑空表" in messages
    assert "图表页" in messages
    assert "纯图片页" in messages
    assert "未覆盖页" in messages
    assert "已标记为无内容" in messages
