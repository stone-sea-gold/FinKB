"""跨页表格缝合的核心验收测试（方案 v2.1 §7.1 验收标准）。"""

from __future__ import annotations

import pytest

from finkb.parser.markdown_blocks import parse_page_anchored_markdown
from finkb.parser.table_stitcher import StitchOptions, stitch_tables

DOC = "002594_2025_年报"
SECTION = "第三节 管理层讨论与分析"


def meta(page: int, section: str | None = SECTION) -> str:
    import json

    payload = {"doc": DOC, "page": page}
    if section:
        payload["section"] = section
    return f"<!-- META: {json.dumps(payload, ensure_ascii=False)} -->"


def stitch(markdown: str, options: StitchOptions | None = None):
    return stitch_tables(parse_page_anchored_markdown(markdown, default_doc=DOC), options)


# --------------------------------------------------------------------------- #
# 1. 两页：无表头续接
# --------------------------------------------------------------------------- #
def test_two_page_headerless_continuation_is_stitched() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 | 2024年 |
| --- | --- | --- |
| 营业收入 | 77,201,000 | 60,231,500 |
| 营业成本 | 61,000,000 | 49,000,000 |

{meta(43)}
| 净利润 | 4,000,000 | 3,000,000 |
| 归母净利润 | 3,900,000 | 2,900,000 |
"""
    result = stitch(markdown)

    assert len(result.tables) == 1
    table = result.tables[0]
    assert table.is_stitched
    assert table.source_tables == 2
    assert (table.page_start, table.page_end) == (42, 43)
    assert table.header == ["项目", "2025年", "2024年"]
    assert len(table.rows) == 4
    assert table.rows[0][0] == "营业收入"
    assert table.rows[3][0] == "归母净利润"
    assert result.records[0].pages == [42, 43]


# --------------------------------------------------------------------------- #
# 2. 三页：链式续接
# --------------------------------------------------------------------------- #
def test_three_page_chain_is_stitched() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |

{meta(43)}
| B | 2 |

{meta(44)}
| C | 3 |
"""
    result = stitch(markdown)

    assert len(result.tables) == 1
    table = result.tables[0]
    assert table.source_tables == 3
    assert (table.page_start, table.page_end) == (42, 44)
    assert [row[0] for row in table.rows] == ["A", "B", "C"]
    assert result.records[0].pages == [42, 43, 44]


# --------------------------------------------------------------------------- #
# 3. 列数不一致：不得缝合
# --------------------------------------------------------------------------- #
def test_column_mismatch_is_not_stitched() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 | 2024年 |
| --- | --- | --- |
| 营业收入 | 1 | 2 |

{meta(43)}
| 其他 | 3 |
"""
    result = stitch(markdown)

    assert len(result.tables) == 2
    assert result.stitched_count == 0
    assert all(t.source_tables == 1 for t in result.tables)


# --------------------------------------------------------------------------- #
# 4. 表格不是页末块：不得缝合
# --------------------------------------------------------------------------- #
def test_table_not_at_page_end_is_not_stitched() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |

{meta(42)}
上表为本节主要数据。另见附注。

{meta(43)}
| B | 2 |
"""
    result = stitch(markdown)

    assert len(result.tables) == 2
    assert result.stitched_count == 0


# --------------------------------------------------------------------------- #
# 5. 下一页重复输出表头：丢弃重复表头后拼接
# --------------------------------------------------------------------------- #
def test_duplicate_header_is_dropped_and_merged() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |

{meta(43)}
| 项目 | 2025年 |
| --- | --- |
| B | 2 |
"""
    result = stitch(markdown)

    assert len(result.tables) == 1
    table = result.tables[0]
    assert table.source_tables == 2
    assert table.header == ["项目", "2025年"]
    assert [row[0] for row in table.rows] == ["A", "B"]
    assert "重复表头" in result.records[0].reason


def test_duplicate_header_can_be_rejected() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |

{meta(43)}
| 项目 | 2025年 |
| --- | --- |
| B | 2 |
"""
    result = stitch(markdown, StitchOptions(allow_duplicate_header=False))
    assert result.stitched_count == 0


# --------------------------------------------------------------------------- #
# 6. 下一页表格自带不同表头：判定为新表
# --------------------------------------------------------------------------- #
def test_new_table_with_own_header_is_not_stitched() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |

{meta(43)}
| 业务板块 | 收入 |
| --- | --- |
| 汽车 | 9 |
"""
    result = stitch(markdown)

    assert len(result.tables) == 2
    assert result.stitched_count == 0


# --------------------------------------------------------------------------- #
# 7. 页首不是表格：不得缝合
# --------------------------------------------------------------------------- #
def test_text_before_next_table_blocks_stitching() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |

{meta(43)}
承接上页数据如下。

| B | 2 |
"""
    result = stitch(markdown)

    assert len(result.tables) == 2
    assert result.stitched_count == 0


# --------------------------------------------------------------------------- #
# 8. ID 与 Markdown 还原
# --------------------------------------------------------------------------- #
def test_table_ids_follow_spec() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |

{meta(43)}
| B | 2 |
"""
    table = stitch(markdown).tables[0]
    assert table.table_id == f"{DOC}#P42_T01"
    assert table.parent_id == f"{DOC}#P42-P43_T01"


def test_single_page_table_keeps_simple_ids() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |
"""
    table = stitch(markdown).tables[0]
    assert table.source_tables == 1
    assert table.parent_id == f"{DOC}#P42_T01"
    assert table.table_id == table.parent_id


def test_stitched_table_renders_back_to_markdown() -> None:
    markdown = f"""{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |

{meta(43)}
| B | 2 |
"""
    rendered = stitch(markdown).tables[0].to_markdown()
    assert rendered.splitlines()[0] == "| 项目 | 2025年 |"
    assert rendered.splitlines()[1].startswith("| ---")
    assert "| B | 2 |" in rendered.splitlines()


# --------------------------------------------------------------------------- #
# 9. 非表格块不受影响
# --------------------------------------------------------------------------- #
def test_non_table_blocks_are_preserved(pages_fixture=None) -> None:
    markdown = f"""{meta(42)}
## 第三节 管理层讨论与分析

{meta(42)}
公司报告期内经营情况如下。

{meta(42)}
| 项目 | 2025年 |
| --- | --- |
| A | 1 |
"""
    pages = parse_page_anchored_markdown(markdown, default_doc=DOC)
    kinds = [block.kind for page in pages for block in page.blocks]
    assert kinds == ["heading", "text", "table"]
    result = stitch_tables(pages)
    assert len(result.tables) == 1


# --------------------------------------------------------------------------- #
# 10. 空输入
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("markdown", ["", "\n\n", "只有文本没有表格"])
def test_no_tables_returns_empty_result(markdown: str) -> None:
    result = stitch(markdown)
    assert result.tables == []
    assert result.stitched_count == 0
