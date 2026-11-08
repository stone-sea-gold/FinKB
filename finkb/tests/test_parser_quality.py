"""解析质量体检（ADR-071）：空表/chart/纯图片页/未覆盖页的统计。

纯离线；用合成 content_list，不读开发语料。
"""

from __future__ import annotations

from finkb.parser.quality import analyze_quality, quality_warnings


def test_analyze_quality_detects_empty_tables_charts_and_image_pages():
    content_list = [
        {"type": "text", "page_idx": 0, "text": "正文"},
        {"type": "table", "page_idx": 1, "table_body": ""},
        {"type": "chart", "page_idx": 2},
        {"type": "image", "page_idx": 3},
        {"type": "header", "page_idx": 3, "text": "页眉"},
        {"type": "table", "page_idx": 5, "table_body": "<table><tr><td>1</td></tr></table>"},
    ]
    quality = analyze_quality(content_list, pdf_pages=6)
    assert quality["pdf_pages"] == 6
    assert quality["parsed_pages"] == 5
    assert quality["empty_table_count"] == 1
    assert quality["empty_tables"][0]["page"] == 2
    assert quality["chart_pages"] == [3]
    assert quality["chart_blocks"] == 1
    assert quality["image_only_pages"] == [4]
    assert quality["missing_pages"] == [5]


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
    assert "空表" in messages
    assert "图表页" in messages
    assert "纯图片页" in messages
    assert "未覆盖页" in messages
