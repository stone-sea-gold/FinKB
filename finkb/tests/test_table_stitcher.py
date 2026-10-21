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


# --------------------------------------------------------------------------- #
# P0：先判表头性质，再比列数（避免「新表」被误报成「列数不符」）
# --------------------------------------------------------------------------- #
def test_new_table_reason_is_not_column_mismatch() -> None:
    """续接表自带表头且列数也不同时，理由必须是「新表」，不是「列数不一致」。"""
    markdown = f"""{meta(42)}
| 项目 | 2025年 | 2024年 |
| --- | --- | --- |
| A | 1 | 2 |

{meta(43)}
| 姓名 | 职务 |
| --- | --- |
| 张三 | 董事 |
"""
    result = stitch(markdown)

    assert result.stitched_count == 0
    # 自带表头 → 确认新表 → 静默，不产生拒绝告警
    assert result.rejections == []


def test_headerless_column_mismatch_is_still_reported() -> None:
    """无表头但列数不符才是真正的「无法确定」，必须留审计。"""
    markdown = f"""{meta(42)}
| 项目 | 2025年 | 2024年 |
| --- | --- | --- |
| A | 1 | 2 |

{meta(43)}
| 说明 | 内容 |
"""
    result = stitch(markdown)

    assert result.stitched_count == 0
    assert len(result.rejections) == 1
    assert "列数不一致" in result.rejections[0].reason
    assert "3 列" in result.rejections[0].reason
    assert "2 列" in result.rejections[0].reason


# --------------------------------------------------------------------------- #
# P1：起始表以合计行收尾 → 表已完整，不再尝试续接
# --------------------------------------------------------------------------- #
def test_table_ending_with_total_is_not_stitched() -> None:
    markdown = f"""{meta(42)}
| 项目 | 金额 |
| --- | --- |
| A | 1 |
| 合计 | 1 |

{meta(43)}
| 说明 | 内容 |
"""
    result = stitch(markdown)

    assert result.stitched_count == 0
    # 表已完整属正常情况，不应产生拒绝告警
    assert result.rejections == []


def test_total_marker_variants_all_stop_stitching() -> None:
    for marker in ("合计", "总计", "小计", "资产总计", "上述三项担保金额合计(D+E+F)"):
        markdown = f"""{meta(42)}
| 项目 | 金额 |
| --- | --- |
| A | 1 |
| {marker} | 1 |

{meta(43)}
| B | 2 |
"""
        result = stitch(markdown)
        assert result.stitched_count == 0, f"{marker} 应判定表已结束"
        assert result.rejections == [], f"{marker} 不应产生告警"


def test_stop_on_total_row_can_be_disabled() -> None:
    markdown = f"""{meta(42)}
| 项目 | 金额 |
| --- | --- |
| A | 1 |
| 合计 | 1 |

{meta(43)}
| B | 2 |
"""
    result = stitch(markdown, StitchOptions(stop_on_total_row=False))

    # 关掉后恢复旧行为：续接表无表头且列数一致 → 缝合
    assert result.stitched_count == 1


def test_total_row_only_counts_last_row() -> None:
    """合计行出现在中间不影响判定——只有末行才算表结束。"""
    markdown = f"""{meta(42)}
| 项目 | 金额 |
| --- | --- |
| 合计 | 1 |
| A | 2 |

{meta(43)}
| B | 3 |
"""
    result = stitch(markdown)

    assert result.stitched_count == 1


def test_is_total_row_helper() -> None:
    from finkb.parser.table_stitcher import is_total_row, _TOTAL_MARKERS

    assert is_total_row(["合计", "1"], _TOTAL_MARKERS)
    assert is_total_row(["资产总计", "", "164,831,252"], _TOTAL_MARKERS)
    assert not is_total_row(["营业收入", "1"], _TOTAL_MARKERS)
    assert not is_total_row([], _TOTAL_MARKERS)
