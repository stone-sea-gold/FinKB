"""端到端：一份极小的 content_list 走完整条解析链。

单元测试各自守一个判据，但判据之间的**顺序**和**副作用**没人守——例如「先缝合再切分」
「caption 在缝合时传下去」。这里用一份手工构造的两页文档把整条链跑一遍，任何一步
改动把结果弄坏，这里会先红。
"""

from __future__ import annotations

import json
from pathlib import Path

from finkb.parser.mineru_client import content_list_to_pages
from finkb.parser.noise_filter import strip_running_noise
from finkb.parser.section_splitter import SplitOptions, split_all
from finkb.parser.table_stitcher import stitch_tables

_FIXTURE = Path(__file__).parent / "fixtures" / "tiny_content_list.json"


def _run():
    items = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    pages = content_list_to_pages(items, "tiny")
    strip_running_noise(pages)
    stitch = stitch_tables(pages)
    return pages, stitch, split_all(stitch.tables, SplitOptions())


def test_pages_and_tables_survive_the_whole_chain() -> None:
    pages, stitch, logical = _run()

    assert len(pages) == 2
    # 两张表表头不同 => 不缝合，但会被当成「疑似续接」检查过一遍
    assert len(stitch.tables) == 2
    assert stitch.stitched_count == 0
    assert [table.kind for table in logical] == ["data", "data"]


def test_caption_and_header_reach_the_stitched_table() -> None:
    _, stitch, _ = _run()
    first = stitch.tables[0]

    assert first.header == ["项目", "金额", "占比(%)"]
    assert first.header_rows == [["项目", "金额", "占比(%)"]]
    assert first.caption == "营业收入构成如下："
    assert first.caption_source == "self"
    # 期间标签被表名挡着，按现行规则不收——这是为 P167 的正确性付的价（见 DEVLOG 坑-36）
    assert first.period_label == ""


def test_values_are_not_shifted_and_totals_are_kept() -> None:
    _, _, logical = _run()
    rows = logical[0].rows

    assert rows[0] == ["整车", "1,000", "60.0"]
    assert rows[-1][0] == "合计"
    assert logical[0].row_range == (0, 4)
    assert logical[0].page_start == 1 and logical[0].page_end == 1
