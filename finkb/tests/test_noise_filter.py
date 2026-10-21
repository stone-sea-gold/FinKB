"""页眉页脚清洗与「缝合跳过噪音块」的验收测试。

对应两个真实缺陷的修复，外加一条护栏：

1. 运行页眉让「第 N+1 页第一个块是表格」这一判据失效，跨页表缝不上；
2. 清洗不可能百分之百彻底，因此缝合还必须能跳过已打标的噪音块；
3. 护栏——跨页表头会在每页重复，绝不能被误当成页眉清掉。
"""

from __future__ import annotations

import json

from finkb.common.models import Block, DocMeta, Page
from finkb.parser.markdown_blocks import (
    parse_markdown_table,
    parse_page_anchored_markdown,
)
from finkb.parser.noise_filter import (
    NoiseConfig,
    detect_signatures,
    drop_noise_blocks,
    line_signature,
    strip_running_noise,
)
from finkb.parser.pipeline import build_tables
from finkb.parser.table_stitcher import stitch_tables

DOC = "002594_2025_年报"
HEADER = "比亚迪股份有限公司 2025 年年度报告全文"
BODIES = ["资产构成重大变动", "公允价值计量", "投资状况分析", "证券投资情况"]


def meta(page: int) -> str:
    return f'<!-- META: {json.dumps({"doc": DOC, "page": page}, ensure_ascii=False)} -->'


def page_with_header(page: int, header: str, body: str) -> str:
    return f"""{meta(page)}
{header}

{meta(page)}
{body}
"""


def pages_of(markdown: str) -> list[Page]:
    return parse_page_anchored_markdown(markdown, default_doc=DOC)


# --------------------------------------------------------------------------- #
# 签名计算
# --------------------------------------------------------------------------- #
def test_signature_ignores_page_numbers() -> None:
    a = line_signature(f"{HEADER}    43")
    b = line_signature(f"{HEADER}    44")
    assert a is not None
    assert a == b


def test_signature_rejects_pure_page_number() -> None:
    assert line_signature("43") is None
    assert line_signature("  - 45 -  ") is None


def test_signature_rejects_overlong_line() -> None:
    assert line_signature("正" * 200) is None


def test_signature_rejects_table_row() -> None:
    """护栏：表格行永不参与噪音判定。"""
    assert line_signature("| 类型 | 代码 | 名称 |") is None
    assert line_signature("| --- | --- | --- |") is None


# --------------------------------------------------------------------------- #
# 检测与剥离
# --------------------------------------------------------------------------- #
def test_running_header_is_detected_and_stripped() -> None:
    markdown = "".join(
        page_with_header(p, f"{HEADER}    {p}", body)
        for p, body in zip((41, 42, 43, 44), BODIES)
    )
    pages = pages_of(markdown)
    report = strip_running_noise(pages)

    assert report.removed_lines == 4
    assert len(report.signatures) == 1
    assert report.affected_pages == [41, 42, 43, 44]
    for page in pages:
        assert page.blocks[0].is_noise
        assert page.blocks[0].lines == []
        assert page.blocks[0].table is None


def test_running_footer_is_detected_and_stripped() -> None:
    markdown = "".join(
        f"""{meta(p)}
{body}

{meta(p)}
{HEADER}  第 {p} 页
"""
        for p, body in zip((41, 42, 43, 44), BODIES)
    )
    pages = pages_of(markdown)
    report = strip_running_noise(pages)

    assert report.removed_lines == 4
    for page in pages:
        assert page.blocks[-1].is_noise


def test_unique_body_lines_are_kept() -> None:
    markdown = "".join(
        page_with_header(p, f"{HEADER}    {p}", body)
        for p, body in zip((41, 42, 43), BODIES)
    )
    pages = pages_of(markdown)
    strip_running_noise(pages)

    for page in pages:
        assert page.blocks[0].is_noise  # 页眉被清掉
        assert len(page.blocks[1].lines) == 1  # 正文保留
        assert not page.blocks[1].is_noise


def test_insufficient_pages_skip_detection() -> None:
    markdown = "".join(
        page_with_header(p, f"{HEADER}    {p}", body)
        for p, body in zip((41, 42), BODIES)
    )
    pages = pages_of(markdown)
    report = strip_running_noise(pages)

    assert report.removed_lines == 0
    assert not report.changed


def test_strip_is_idempotent() -> None:
    markdown = "".join(
        page_with_header(p, f"{HEADER}    {p}", body)
        for p, body in zip((41, 42, 43), BODIES)
    )
    pages = pages_of(markdown)
    first = strip_running_noise(pages)
    second = strip_running_noise(pages)

    assert first.removed_lines == 3
    assert second.removed_lines == 0


def test_single_occurrence_signature_is_not_noise() -> None:
    """阈值硬下限为 2：只出现一次的正文行绝不能被判为噪音。"""
    markdown = "".join(
        page_with_header(p, f"{HEADER}    {p}", body)
        for p, body in zip((41, 42, 43, 44, 45), BODIES + ["第五页独有内容"])
    )
    pages = pages_of(markdown)
    counter = detect_signatures(pages, NoiseConfig())

    header_sig = line_signature(f"{HEADER}    41")
    assert counter[header_sig] == 5
    assert counter[line_signature("第五页独有内容")] == 1


def test_repeating_table_header_is_not_stripped() -> None:
    """跨页表头在每页重复出现，必须原样保留。"""
    markdown = "".join(
        f"""{meta(p)}
{HEADER}   {p}

{meta(p)}
| 类型 | 代码 | 名称 |
| --- | --- | --- |
| 项目{p} | 0{p} | 名{p} |
"""
        for p in (41, 42, 43, 44)
    )
    pages = pages_of(markdown)
    strip_running_noise(pages)

    for page in pages:
        table_block = page.blocks[1]
        assert table_block.table is not None
        assert table_block.table.header == ["类型", "代码", "名称"]
        assert len(table_block.table.rows) == 1


def test_drop_noise_blocks_removes_them() -> None:
    markdown = "".join(
        page_with_header(p, f"{HEADER}    {p}", body)
        for p, body in zip((41, 42, 43), BODIES)
    )
    pages = pages_of(markdown)
    strip_running_noise(pages)

    removed = drop_noise_blocks(pages)

    assert removed == 3
    assert all(len(page.blocks) == 1 for page in pages)
    assert all(not page.blocks[0].is_noise for page in pages)


# --------------------------------------------------------------------------- #
# 修复一：页眉不再阻断跨页缝合
# --------------------------------------------------------------------------- #
def test_stitching_succeeds_after_header_removal() -> None:
    markdown = f"""{meta(43)}
| 类型 | 代码 | 名称 |
| --- | --- | --- |
| 境内外股票 | 02676 | 纳芯微 |

{page_with_header(44, f"{HEADER}    44", "| 债券 | 019742 | 24 特国 01 |")}
{page_with_header(45, f"{HEADER}    45", "无关内容甲")}
"""
    result = build_tables(markdown, DOC)

    stitched = [t for t in result.tables if t.is_stitched]
    assert len(stitched) == 1
    table = stitched[0]
    assert (table.page_start, table.page_end) == (43, 44)
    assert table.source_tables == 2
    assert [row[0] for row in table.rows] == ["境内外股票", "债券"]
    assert result.noise.removed_lines >= 2


# --------------------------------------------------------------------------- #
# 修复二：清洗不彻底时，缝合仍能跳过已打标的噪音块
# --------------------------------------------------------------------------- #
def test_stitcher_skips_noise_block_without_stripping() -> None:
    head = Block(meta=DocMeta(DOC, 43), kind="table", lines=[], block_index=0)
    head.table = parse_markdown_table(
        ["| 类型 | 代码 |", "| --- | --- |", "| 境内外股票 | 02676 |"],
        DocMeta(DOC, 43),
    )
    pages = [Page(number=43, blocks=[head])]

    header_block = Block(meta=DocMeta(DOC, 44), kind="text", lines=[], block_index=0)
    header_block.is_noise = True
    continuation = Block(meta=DocMeta(DOC, 44), kind="table", lines=[], block_index=1)
    continuation.table = parse_markdown_table(
        ["| 债券 | 019742 |"], DocMeta(DOC, 44)
    )
    pages.append(Page(number=44, blocks=[header_block, continuation]))

    result = stitch_tables(pages)

    assert result.stitched_count == 1
    assert result.tables[0].page_end == 44


def test_noise_block_at_page_tail_does_not_block_stitching() -> None:
    """页尾噪音块（页脚）同理：缝合看最后一个非噪音块。"""
    markdown = f"""{meta(43)}
| 类型 | 代码 |
| --- | --- |
| 境内外股票 | 02676 |
"""
    pages = pages_of(markdown)
    footer = Block(meta=DocMeta(DOC, 43), kind="text", lines=[], block_index=1)
    footer.is_noise = True
    pages[0].blocks.append(footer)

    continuation = Block(meta=DocMeta(DOC, 44), kind="table", lines=[])
    continuation.table = parse_markdown_table(["| 债券 | 019742 |"], DocMeta(DOC, 44))
    pages.append(Page(number=44, blocks=[continuation]))

    assert stitch_tables(pages).stitched_count == 1


# --------------------------------------------------------------------------- #
# 流水线
# --------------------------------------------------------------------------- #
def test_pipeline_summary_reports_all_stages() -> None:
    markdown = "".join(
        page_with_header(p, f"{HEADER}    {p}", body)
        for p, body in zip((41, 42, 43), BODIES)
    )
    result = build_tables(markdown, DOC)
    summary = result.summary()

    assert "页数=3" in summary
    assert "剥离噪音行=3" in summary


# --------------------------------------------------------------------------- #
# 修复三：页眉正文被清后，残留的独立页码行也要清掉
# --------------------------------------------------------------------------- #
def test_standalone_page_number_line_is_stripped() -> None:
    markdown = "".join(
        f"""{meta(p)}
{HEADER}   {p}

{meta(p)}
{body}
"""
        for p, body in zip((41, 42, 43), BODIES)
    )
    pages = pages_of(markdown)
    strip_running_noise(pages)

    for page in pages:
        assert page.blocks[0].is_noise
        assert all(line.strip() != str(page.number) for line in page.blocks[0].lines)


def test_page_number_on_separate_line_is_stripped() -> None:
    """模拟 pypdf 输出：页眉文字与页码分成两行。"""
    markdown = "".join(
        f"""{meta(p)}
{HEADER}

{p}

{meta(p)}
{body}
"""
        for p, body in zip((41, 42, 43), BODIES)
    )
    pages = pages_of(markdown)
    strip_running_noise(pages)

    for page in pages:
        assert page.blocks[0].is_noise


def test_page_number_mismatch_is_kept() -> None:
    """数字不等于本页页码时不能删——可能是正文里的真实数值。"""
    markdown = "".join(
        f"""{meta(p)}
{HEADER}

999

{meta(p)}
{body}
"""
        for p, body in zip((41, 42, 43), BODIES)
    )
    pages = pages_of(markdown)
    strip_running_noise(pages)

    for page in pages:
        assert "999" in page.blocks[0].lines


def test_page_number_deep_inside_body_is_kept() -> None:
    """页码识别只在文本块头尾极小范围内启用。"""
    body_lines = "\n".join(["甲", "乙", "丙", "丁", "43", "戊", "己"])
    markdown = "".join(
        f"""{meta(p)}
{HEADER}

{meta(p)}
{body_lines}
"""
        for p in (41, 42, 43)
    )
    pages = pages_of(markdown)
    strip_running_noise(pages)

    assert "43" in pages[0].blocks[1].lines
