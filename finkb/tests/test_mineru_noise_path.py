"""MinerU 路径（content_list.json）下的噪音清洗与缝合验收测试。

⚠️ 这份测试是为了补上一个真实的验证盲区：

此前的噪音测试全部走 ``parse_page_anchored_markdown``（Markdown 锚定格式），
真实数据验证又走的是 ``OfflinePdfParser``（pypdf）。但生产路径是 MinerU 的
``content_list.json`` —— 它的块粒度与这两者都不同：

- pypdf：**每页 1 个块**，页眉是块内的第一**行**；
- MinerU：**每页 N 个块**，页眉是独立的**块**。

块粒度变了，清洗与缝合的行为就必须重新验证，不能直接沿用 pypdf 的结论。

另外这里也钉住一个集成缺陷：MinerU 对跨页续接表输出的 HTML **没有 ``<th>``**，
若沿用「首行即表头」的规则，续接行会被误判为新表而拒绝拼接。
"""

from __future__ import annotations

from finkb.parser.mineru_client import content_list_to_pages
from finkb.parser.noise_filter import strip_running_noise
from finkb.parser.table_stitcher import first_content_index, stitch_tables

DOC = "002594_2025_年报"
HEADER = "比亚迪股份有限公司 2025 年年度报告全文"
BODIES = ["资产构成重大变动", "公允价值计量", "投资状况分析"]


def html_table(rows: list[list[str]], with_header: bool) -> str:
    """构造 MinerU 风格 table_body。

    ``with_header=True`` 时首行用 ``<th>``，否则全部用 ``<td>``——
    这与 MinerU 对「新表 / 续接表」的真实输出形态一致。
    """
    parts: list[str] = []
    for index, row in enumerate(rows):
        tag = "th" if (with_header and index == 0) else "td"
        cells = "".join(f"<{tag}>{cell}</{tag}>" for cell in row)
        parts.append(f"<tr>{cells}</tr>")
    return "<table>" + "".join(parts) + "</table>"


def page_item(text: str, page_no: int) -> dict:
    """构造一个文本条目。``page_idx`` 为 0-based，物理页码 = page_idx + 1。"""
    return {"type": "text", "text": text, "page_idx": page_no - 1}


def build_content_list() -> list[dict]:
    """4 页（物理页码 41–44）：41/42 正文，43 带表头表，44 无表头续接行。"""
    items: list[dict] = []
    for offset, body in enumerate(BODIES[:2]):
        page_no = 41 + offset
        items.append(page_item(f"{HEADER}    {page_no}", page_no))
        items.append(page_item(body, page_no))

    # 第 43 页：带表头的表
    items.append(page_item(f"{HEADER}    43", 43))
    items.append(
        {
            "type": "table",
            "page_idx": 42,
            "table_body": html_table(
                [["类型", "代码", "名称"], ["境内外股票", "02676", "纳芯微"]], True
            ),
        }
    )

    # 第 44 页：无表头续接行
    items.append(page_item(f"{HEADER}    44", 44))
    items.append(
        {
            "type": "table",
            "page_idx": 43,
            "table_body": html_table([["债券", "019742", "24 特国 01"]], False),
        }
    )
    return items


# --------------------------------------------------------------------------- #
# 前提：MinerU 路径下页眉确实是独立块
# --------------------------------------------------------------------------- #
def test_header_appears_as_separate_block_per_page() -> None:
    pages = content_list_to_pages(build_content_list(), DOC)

    assert [p.number for p in pages] == [41, 42, 43, 44]
    for page in pages:
        assert page.blocks[0].text.startswith("比亚迪股份有限公司")
        assert page.blocks[0].is_noise is False  # 清洗前尚未打标


def test_header_signature_is_detected_on_mineru_path() -> None:
    pages = content_list_to_pages(build_content_list(), DOC)
    report = strip_running_noise(pages)

    assert report.removed_lines == 4
    assert len(report.affected_pages) == 4
    for page in pages:
        assert page.blocks[0].is_noise


def test_stitching_works_on_mineru_path_after_cleaning() -> None:
    pages = content_list_to_pages(build_content_list(), DOC)
    strip_running_noise(pages)

    page44 = next(p for p in pages if p.number == 44)
    assert first_content_index(page44) == 1

    result = stitch_tables(pages)
    assert result.stitched_count == 1
    table = result.tables[0]
    assert (table.page_start, table.page_end) == (43, 44)
    assert [row[0] for row in table.rows] == ["境内外股票", "债券"]


# --------------------------------------------------------------------------- #
# 集成缺陷：MinerU 续接表 HTML 无 <th>，不能被当成表头
# --------------------------------------------------------------------------- #
def test_table_without_th_has_no_header() -> None:
    pages = content_list_to_pages(
        [
            {
                "type": "table",
                "page_idx": 0,
                "table_body": html_table([["债券", "019742"]], False),
            }
        ],
        DOC,
    )
    table = pages[0].blocks[0].table

    assert table is not None
    assert table.header is None
    assert table.is_headerless
    assert table.rows == [["债券", "019742"]]


def test_table_with_th_keeps_header() -> None:
    pages = content_list_to_pages(
        [
            {
                "type": "table",
                "page_idx": 0,
                "table_body": html_table([["类型", "代码"], ["债券", "019742"]], True),
            }
        ],
        DOC,
    )
    table = pages[0].blocks[0].table

    assert table.header == ["类型", "代码"]
    assert table.rows == [["债券", "019742"]]


# --------------------------------------------------------------------------- #
# 页码作为独立块的情况
# --------------------------------------------------------------------------- #
def test_page_number_as_separate_block_is_stripped() -> None:
    items: list[dict] = []
    for offset in range(3):
        page_no = 41 + offset
        items.append(page_item(HEADER, page_no))
        items.append(page_item(str(page_no), page_no))
        items.append(page_item(BODIES[offset], page_no))

    pages = content_list_to_pages(items, DOC)
    strip_running_noise(pages)

    for page in pages:
        remaining = [b.text for b in page.blocks if not b.is_noise]
        assert str(page.number) not in remaining, f"P{page.number} 的页码块未清除"


# --------------------------------------------------------------------------- #
# 页眉与页码合并在同一块（MinerU 常见形态）
# --------------------------------------------------------------------------- #
def test_header_and_page_number_in_one_block_is_fully_stripped() -> None:
    items: list[dict] = []
    for offset in range(3):
        page_no = 41 + offset
        items.append(page_item(f"{HEADER}    {page_no}", page_no))
        items.append(page_item(BODIES[offset], page_no))

    pages = content_list_to_pages(items, DOC)
    strip_running_noise(pages)

    for page in pages:
        assert page.blocks[0].is_noise
        assert page.blocks[0].lines == []


# --------------------------------------------------------------------------- #
# 实测修正（2026-10-21，真实 MinerU 输出）：页码与类型标签
# --------------------------------------------------------------------------- #
def test_page_number_type_overrides_renumbered_page_idx() -> None:
    """分页解析时 MinerU 的 page_idx 会从 0 重编号，必须用 page_number 块修正。

    否则所有 [Ref: doc#P1] 锚点都会指错页——对「物理页码强溯源」是致命错误。
    """
    content_list = [
        {"type": "text", "text": "正文", "page_idx": 0},
        {"type": "page_number", "text": "43", "page_idx": 0},
        {"type": "text", "text": "第二页正文", "page_idx": 1},
        {"type": "page_number", "text": "44", "page_idx": 1},
    ]
    pages = content_list_to_pages(content_list, DOC)

    assert [p.number for p in pages] == [43, 44]


def test_page_idx_used_when_page_number_missing() -> None:
    pages = content_list_to_pages(
        [{"type": "text", "text": "正文", "page_idx": 0}], DOC
    )
    assert [p.number for p in pages] == [1]


def test_header_and_page_number_types_are_marked_noise() -> None:
    """MinerU 不过滤页眉页脚，而是打类型标签——直接据此标记，不靠统计推断。"""
    content_list = [
        {"type": "text", "text": "正文甲", "page_idx": 0},
        {"type": "header", "text": HEADER, "page_idx": 0},
        {"type": "page_number", "text": "43", "page_idx": 0},
    ]
    pages = content_list_to_pages(content_list, DOC)
    blocks = pages[0].blocks

    assert blocks[0].is_noise is False
    assert blocks[1].is_noise is True
    assert blocks[2].is_noise is True


def test_rowspan_merged_header_is_detected() -> None:
    """MinerU 用 <td rowspan="2"> 做表头，不是 <th>。"""
    html = (
        '<table><tr>'
        '<td rowspan="2">证券品种</td><td rowspan="2">证券代码</td>'
        '</tr><tr><td></td></tr>'
        '<tr><td>境内外股票</td><td>301358</td></tr></table>'
    )
    pages = content_list_to_pages(
        [{"type": "table", "page_idx": 0, "table_body": html}], DOC
    )
    table = pages[0].blocks[0].table

    assert table.header == ["证券品种", "证券代码"]
    assert table.rows == [["境内外股票", "301358"]]


def test_rowspan_artifact_empty_row_is_dropped() -> None:
    """rowspan 未展开时会留下「1 个空单元格」的垃圾行，必须剔除。"""
    html = (
        '<table><tr><td rowspan="2">A</td><td rowspan="2">B</td></tr>'
        "<tr><td></td></tr>"
        "<tr><td>1</td><td>2</td></tr></table>"
    )
    pages = content_list_to_pages(
        [{"type": "table", "page_idx": 0, "table_body": html}], DOC
    )
    table = pages[0].blocks[0].table

    assert table.rows == [["1", "2"]]
    assert all(any(cell.strip() for cell in row) for row in table.rows)


def test_stitch_rejection_is_recorded() -> None:
    """列数不符被拒绝时，必须留下审计记录，否则无法排查。"""
    pages = content_list_to_pages(
        [
            {
                "type": "table",
                "page_idx": 0,
                "table_body": html_table(
                    [["品种", "代码", "名称"], ["股票", "1", "甲"]], True
                ),
            },
            {
                "type": "table",
                "page_idx": 1,
                "table_body": html_table([["债券", "2"]], False),
            },
        ],
        DOC,
    )
    result = stitch_tables(pages)

    assert result.stitched_count == 0
    assert len(result.rejections) == 1
    assert "列数不一致" in result.rejections[0].reason
