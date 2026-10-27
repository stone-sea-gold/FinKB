"""MinerU 客户端、content_list 转换与离线兜底解析器的测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finkb.common.models import Block, DocMeta, Page
from finkb.parser.mineru_client import (
    MineruClient,
    MineruConfig,
    MineruError,
    OfflinePdfParser,
    content_list_to_pages,
    render_page_anchored_markdown,
)
from finkb.parser.markdown_blocks import parse_page_anchored_markdown

DOC = "002594_2025_年报"


# --------------------------------------------------------------------------- #
# content_list.json → Page IR
# --------------------------------------------------------------------------- #
def test_content_list_page_idx_is_converted_to_one_based() -> None:
    content_list = [
        {"type": "text", "text": "第一页正文", "page_idx": 0},
        {"type": "text", "text": "第二页正文", "page_idx": 1},
    ]
    pages = content_list_to_pages(content_list, DOC)
    assert [p.number for p in pages] == [1, 2]


TABLE_HTML = (
    "<table><tr><td>项目</td><td>金额</td></tr>"
    "<tr><td>营业收入</td><td>1,000</td></tr></table>"
)


def _only_table_block(content_list: list[dict[str, object]]):
    pages = content_list_to_pages(content_list, DOC)
    (block,) = [block for page in pages for block in page.blocks if block.table is not None]
    assert block.table is not None
    return block.table


def test_heading_run_before_table_becomes_its_header() -> None:
    """P242：MinerU 把表头两个单元格拆成了表格前的两个二级标题块。"""
    content_list = [
        {"type": "text", "text": "4、其他关联方", "text_level": 2, "page_idx": 241},
        {"type": "text", "text": "公司名称", "text_level": 2, "page_idx": 241},
        {"type": "text", "text": "关联方关系", "text_level": 2, "page_idx": 241},
        {
            "type": "table",
            "table_body": (
                "<table><tr><td>深圳壳牌比亚迪电动汽车投资有限公司</td>"
                "<td>联营企业</td></tr></table>"
            ),
            "page_idx": 241,
        },
    ]
    table = _only_table_block(content_list)

    assert table.header == ["公司名称", "关联方关系"]
    assert table.rows == [["深圳壳牌比亚迪电动汽车投资有限公司", "联营企业"]]


def test_heading_run_with_wrong_cell_count_is_not_a_header() -> None:
    """章节标题不是表头：格数对不上就放弃，不能硬凑。"""
    content_list = [
        {"type": "text", "text": "十三、内部控制评价报告", "text_level": 2, "page_idx": 74},
        {"type": "text", "text": "1、内控评价报告", "text_level": 2, "page_idx": 74},
        {
            "type": "table",
            "table_body": (
                "<table><tr><td>缺陷等级</td><td>设定标准</td><td>缺陷认定标准</td>"
                "<td>整改措施</td></tr><tr><td>重大缺陷</td><td>甲</td><td>乙</td>"
                "<td>丙</td></tr></table>"
            ),
            "page_idx": 74,
        },
    ]
    table = _only_table_block(content_list)

    assert table.header is None


def test_heading_run_breaks_on_intervening_text() -> None:
    """标题与表格之间夹了正文，就不是这张表的表头。"""
    content_list = [
        {"type": "text", "text": "公司名称", "text_level": 2, "page_idx": 0},
        {"type": "text", "text": "与本集团发生交易的合营企业和联营企业如下：", "page_idx": 0},
        {
            "type": "table",
            "table_body": "<table><tr><td>甲公司</td><td>联营企业</td></tr></table>",
            "page_idx": 0,
        },
    ]
    table = _only_table_block(content_list)

    assert table.header is None


def test_empty_table_placeholder_extends_page_end() -> None:
    """MinerU 把跨页表合并成一块锚定在首页，后续页只留空 table 块。"""
    content_list = [
        {"type": "text", "text": "十七、其他重大事项的说明", "page_idx": 93},
        {"type": "table", "table_body": TABLE_HTML, "page_idx": 93},
        {"type": "table", "table_body": "", "page_idx": 94},
        {"type": "table", "table_body": "", "page_idx": 95},
    ]
    table = _only_table_block(content_list)

    assert (table.page, table.last_page) == (94, 96)


def test_empty_table_on_same_page_does_not_extend() -> None:
    content_list = [
        {"type": "table", "table_body": TABLE_HTML, "page_idx": 0},
        {"type": "table", "table_body": "", "page_idx": 0},
    ]
    table = _only_table_block(content_list)

    assert table.last_page == 1


def test_empty_table_with_page_gap_does_not_extend() -> None:
    """占位页不连续说明中间断过，不能盲目接上。"""
    content_list = [
        {"type": "table", "table_body": TABLE_HTML, "page_idx": 0},
        {"type": "table", "table_body": "", "page_idx": 2},
    ]
    table = _only_table_block(content_list)

    assert table.last_page == 1


def test_placeholder_belongs_to_the_table_above_it_on_the_same_page() -> None:
    """P65 形态：页首占位块续上一页的表，页中的实块才是新表。"""
    content_list = [
        {"type": "table", "table_body": TABLE_HTML, "page_idx": 0},
        {"type": "table", "table_body": "", "page_idx": 1},
        {"type": "table", "table_body": TABLE_HTML, "page_idx": 1},
        {"type": "table", "table_body": "", "page_idx": 2},
    ]
    pages = content_list_to_pages(content_list, DOC)
    tables = [block.table for page in pages for block in page.blocks if block.table is not None]

    assert [table.last_page for table in tables] == [2, 3]


def test_content_list_heading_sets_section_for_following_blocks() -> None:
    content_list = [
        {"type": "text", "text": "封面", "page_idx": 0},
        {"type": "title", "text": "第三节 管理层讨论与分析", "page_idx": 3},
        {"type": "text", "text": "报告期内经营情况", "page_idx": 3},
    ]
    pages = content_list_to_pages(content_list, DOC)
    target = pages[-1]
    assert target.blocks[0].meta.section == "第三节 管理层讨论与分析"
    assert target.blocks[1].meta.section == "第三节 管理层讨论与分析"
    assert target.blocks[1].meta.page == 4


def test_content_list_table_body_html_becomes_pipe_table() -> None:
    content_list = [
        {
            "type": "table",
            "page_idx": 41,
            "table_body": "<table><tr><th>项目</th><th>2025年</th></tr>"
            "<tr><td>营业收入</td><td>77,201,000</td></tr></table>",
        }
    ]
    pages = content_list_to_pages(content_list, DOC)
    block = pages[0].blocks[0]
    assert block.kind == "table"
    assert block.text.splitlines()[0] == "| 项目 | 2025年 |"
    assert "| 营业收入 | 77,201,000 |" in block.text


def test_content_list_skips_empty_text() -> None:
    pages = content_list_to_pages(
        [{"type": "text", "text": "   ", "page_idx": 0}], DOC
    )
    assert pages == []


# --------------------------------------------------------------------------- #
# 锚定 Markdown 往返
# --------------------------------------------------------------------------- #
def test_render_round_trip_preserves_pages_and_sections() -> None:
    pages = [
        Page(
            number=42,
            blocks=[
                Block(
                    meta=DocMeta(DOC, 42, "第三节 管理层讨论与分析"),
                    kind="text",
                    lines=["报告期内经营情况"],
                )
            ],
        )
    ]
    markdown = render_page_anchored_markdown(pages)
    assert "<!-- META:" in markdown
    assert '"page": 42' in markdown

    reparsed = parse_page_anchored_markdown(markdown, default_doc=DOC)
    assert len(reparsed) == 1
    assert reparsed[0].number == 42
    assert reparsed[0].blocks[0].meta.section == "第三节 管理层讨论与分析"


def test_render_skips_noise_blocks() -> None:
    """页眉页脚已判 ``is_noise``，落盘时不该再出现在正文里。

    MinerU 把页眉标成 ``header``、页脚标成 ``footer``，``_NOISE_TYPES`` 已经
    给它们打了 ``is_noise=True``；但渲染时不分青红皂白全写进去，比亚迪年报
    的 ``page_anchored.md`` 里因此有 267 行「…2025年年度报告全文」。
    """
    pages = [
        Page(
            number=3,
            blocks=[
                Block(
                    meta=DocMeta(DOC, 3),
                    kind="header",
                    lines=["比亚迪股份有限公司 2025年年度报告全文"],
                    is_noise=True,
                ),
                Block(meta=DocMeta(DOC, 3), kind="text", lines=["第一节 重要提示"]),
                Block(meta=DocMeta(DOC, 3), kind="page_number", lines=["3"], is_noise=True),
            ],
        )
    ]
    markdown = render_page_anchored_markdown(pages)

    assert "第一节 重要提示" in markdown
    assert "2025年年度报告全文" not in markdown
    assert "<!-- META:" in markdown


# --------------------------------------------------------------------------- #
# 客户端流程（使用假传输层，不触网）
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, content: bytes = b""):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)
        self.content = content

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("headers")))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs.get("headers")))
        return self.responses.pop(0)

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs.get("headers")))
        return self.responses.pop(0)


def test_submit_url_returns_task_id_and_sends_token() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {"task_id": "t-001"}})])
    client = MineruClient(MineruConfig(token="secret-token"), session=session)

    task_id = client.submit_url("https://example.com/a.pdf")

    assert task_id == "t-001"
    method, url, headers = session.calls[0]
    assert method == "POST"
    assert url.endswith(MineruConfig.submit_path)
    assert headers["Authorization"] == "Bearer secret-token"


def test_wait_for_result_polls_until_done() -> None:
    session = FakeSession(
        [
            FakeResponse({"code": 0, "data": {"state": "running"}}),
            FakeResponse(
                {"code": 0, "data": {"state": "done", "full_zip_url": "https://x/r.zip"}}
            ),
        ]
    )
    client = MineruClient(
        MineruConfig(token="t", poll_interval=0, poll_timeout=5), session=session
    )

    assert client.wait_for_result("t-001") == "https://x/r.zip"


def test_wait_for_result_raises_on_failure_state() -> None:
    session = FakeSession(
        [FakeResponse({"code": 0, "data": {"state": "failed", "err_msg": "bad pdf"}})]
    )
    client = MineruClient(MineruConfig(token="t", poll_interval=0), session=session)

    with pytest.raises(MineruError, match="bad pdf"):
        client.wait_for_result("t-001")


def test_submit_url_raises_when_task_id_missing() -> None:
    session = FakeSession([FakeResponse({"code": -1, "data": {}})])
    client = MineruClient(MineruConfig(token="t"), session=session)

    with pytest.raises(MineruError, match="task_id"):
        client.submit_url("https://example.com/a.pdf")


# --------------------------------------------------------------------------- #
# 离线兜底解析器（真实 PDF，不触网）
# --------------------------------------------------------------------------- #
def _make_pdf(page_texts: list[str]) -> bytes:
    """构造最小可用 PDF（Helvetica + Tj），用于验证 pypdf 兜底链路。"""
    n = len(page_texts)
    font_num = 3 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode("ascii"),
    ]
    for i, text in enumerate(page_texts):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 100 Td ({escaped}) Tj ET".encode("ascii")
        objects.append(
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
                b"/Resources << /Font << /F1 " + str(font_num).encode() + b" 0 R >> >> "
                b"/Contents " + str(4 + 2 * i).encode() + b" 0 R >>"
            )
        )
        objects.append(
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    payload = b"%PDF-1.4\n"
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload += b"%d 0 obj\n" % number + value + b"\nendobj\n"

    xref_offset = len(payload)
    payload += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    payload += b"".join(b"%010d 00000 n \n" % offset for offset in offsets[1:])
    payload += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
    payload += b"startxref\n" + str(xref_offset).encode() + b"\n%%EOF\n"
    return payload


def test_offline_parser_extracts_two_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(_make_pdf(["PageOne Revenue 77201000", "PageTwo Cost 61000000"]))

    document = OfflinePdfParser(DOC).parse(pdf_path)

    assert document.source == "pypdf-fallback"
    assert len(document.pages) == 2
    assert [p.number for p in document.pages] == [1, 2]
    assert "PageOne" in document.pages[0].blocks[0].text
    assert "PageTwo" in document.pages[1].blocks[0].text


# --------------------------------------------------------------------------- #
# 按实测规范修正：标题判定用 text_level，而不是 type
# --------------------------------------------------------------------------- #
def test_heading_detected_via_text_level_field() -> None:
    """content_list 中标题是 type=text + text_level，不是 type=title。"""
    content_list = [
        {"type": "text", "text": "第三节 管理层讨论与分析", "text_level": 1, "page_idx": 3},
        {"type": "text", "text": "报告期内经营情况如下。", "page_idx": 3},
    ]
    pages = content_list_to_pages(content_list, DOC)
    blocks = pages[0].blocks

    assert blocks[0].kind == "heading"
    assert blocks[1].kind == "text"
    assert blocks[1].meta.section == "第三节 管理层讨论与分析"


def test_body_paragraph_has_no_text_level_and_stays_text() -> None:
    content_list = [{"type": "text", "text": "正文段落", "page_idx": 0}]
    pages = content_list_to_pages(content_list, DOC)
    assert pages[0].blocks[0].kind == "text"
    assert pages[0].blocks[0].meta.section is None


def test_layout_json_style_title_type_still_supported() -> None:
    content_list = [{"type": "title", "text": "第一节 公司简介", "page_idx": 0}]
    pages = content_list_to_pages(content_list, DOC)
    assert pages[0].blocks[0].kind == "heading"


# --------------------------------------------------------------------------- #
# 按实测规范修正：本地文件走预签名上传，且 PUT 不得携带任何 header
# --------------------------------------------------------------------------- #
def test_request_upload_urls_returns_batch_and_url() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "batch_id": "b-123",
                        "file_urls": ["https://oss.example.com/upload?sig=x"],
                    },
                }
            )
        ]
    )
    client = MineruClient(MineruConfig(token="t"), session=session)

    batch_id, upload_url = client.request_upload_urls("002594_2025_年报.pdf", "002594")

    assert batch_id == "b-123"
    assert upload_url.startswith("https://oss.example.com/upload")
    method, url, headers = session.calls[0]
    assert method == "POST"
    assert url.endswith(MineruConfig.upload_urls_path)
    assert headers["Authorization"] == "Bearer t"


def test_upload_file_sends_no_headers(tmp_path) -> None:
    """PUT 预签名 URL 绝不能带 header，否则 OSS 返回 403。"""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    session = FakeSession([FakeResponse({"ok": True})])
    client = MineruClient(MineruConfig(token="t"), session=session)

    client.upload_file("https://oss.example.com/upload?sig=x", pdf)

    method, url, headers = session.calls[0]
    assert method == "PUT"
    assert headers is None


def test_wait_for_batch_polls_until_done() -> None:
    session = FakeSession(
        [
            FakeResponse({"code": 0, "data": {"extract_result": [{"state": "pending"}]}}),
            FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"state": "done", "full_zip_url": "https://x/b.zip"}
                        ]
                    },
                }
            ),
        ]
    )
    client = MineruClient(
        MineruConfig(token="t", poll_interval=0, poll_timeout=5), session=session
    )

    assert client.wait_for_batch("b-123") == "https://x/b.zip"


def test_wait_for_batch_raises_on_failed() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "extract_result": [{"state": "failed", "err_msg": "额度用尽"}]
                    },
                }
            )
        ]
    )
    client = MineruClient(MineruConfig(token="t", poll_interval=0), session=session)

    with pytest.raises(MineruError, match="额度用尽"):
        client.wait_for_batch("b-123")


def test_request_upload_urls_raises_when_incomplete() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {"batch_id": "b"}})])
    client = MineruClient(MineruConfig(token="t"), session=session)

    with pytest.raises(MineruError, match="file_urls"):
        client.request_upload_urls("a.pdf")


def test_standalone_period_label_is_attached_to_the_table() -> None:
    """P167：MinerU 把「2025 年」输出成表格前的一个独立文本块。

    同一页两张表头完全相同的表（T94/T95）只有靠这个标签才分得出年份。
    """
    content_list = [
        {"type": "text", "text": "（2） 按坏账计提方法分类披露", "text_level": 2, "page_idx": 0},
        {"type": "text", "text": "2025 年", "page_idx": 0},
        {
            "type": "table",
            "table_body": "<table><tr><td></td><td>金额</td></tr><tr><td>合计</td><td>1</td></tr></table>",
            "page_idx": 0,
        },
    ]
    table = _only_table_block(content_list)

    assert table.period_label == "2025 年"


def test_period_label_must_be_adjacent() -> None:
    """中间夹了别的正文，标签就不属于这张表。"""
    content_list = [
        {"type": "text", "text": "2025 年", "page_idx": 0},
        {"type": "text", "text": "单位：千元", "page_idx": 0},
        {
            "type": "table",
            "table_body": "<table><tr><td>合计</td><td>1</td></tr></table>",
            "page_idx": 0,
        },
    ]

    assert _only_table_block(content_list).period_label == ""


def test_non_period_text_before_table_is_not_a_label() -> None:
    """只有「年份（可带 12 月 31 日）」才算期间标签，别的短文本不算。"""
    content_list = [
        {"type": "text", "text": "单位：千元", "page_idx": 0},
        {
            "type": "table",
            "table_body": "<table><tr><td>合计</td><td>1</td></tr></table>",
            "page_idx": 0,
        },
    ]

    assert _only_table_block(content_list).period_label == ""
