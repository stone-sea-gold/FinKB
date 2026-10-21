"""页码锚定 Markdown 的解析与表格识别。

本模块负责把 L0 的中间格式（带 ``<!-- META: {...} -->`` 的 Markdown）解析成
按页组织的 ``Page``/``Block``/``TableBlock`` 结构，是 ``table_stitcher`` 的直接上游。

锚定格式（方案 §7.1）::

    <!-- META: {"doc":"002594_2025_年报","page":42,"section":"第三节 管理层讨论与分析"} -->
    | 项目 | 2025年 | 2024年 |
    | --- | --- | --- |
    | 营业收入 | 77,201,000 | 60,231,500 |

一个 META 注释开启一个新块，块内容持续到下一个 META 或文件结束。
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

from finkb.common.models import Block, DocMeta, Page, TableBlock

_META_RE = re.compile(r"^\s*<!--\s*META:\s*(?P<payload>\{.*\})\s*-->\s*$")
_SEPARATOR_CELL_RE = re.compile(r"^:?-{1,}:?$")


# --------------------------------------------------------------------------- #
# 表格行解析
# --------------------------------------------------------------------------- #
def split_markdown_row(line: str) -> list[str]:
    """切分一行 Markdown 管道表，正确处理转义 ``\\|``。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells = re.split(r"(?<!\\)\|", text)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def is_separator_row(line: str) -> bool:
    """判断是否为 Markdown 表格分隔行（``|---|---|``）。"""
    if "-" not in line:
        return False
    cells = split_markdown_row(line)
    if not cells:
        return False
    return all(_SEPARATOR_CELL_RE.match(cell.strip()) for cell in cells)


def looks_like_table_line(line: str) -> bool:
    """粗判一行是否属于表格（用于把连续行归并为一个表格块）。"""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("|"):
        return True
    return is_separator_row(stripped)


def parse_markdown_table(
    lines: list[str],
    meta: DocMeta,
    block_index: int = 0,
) -> TableBlock:
    """把一组 Markdown 表格行解析为 :class:`TableBlock`。

    表头判定规则：
    - 第 2 行是分隔行 → 第 1 行为表头；
    - 否则视为「无表头表格」，首行即数据行（跨页续接的关键信号）。
    """
    cleaned = [line for line in lines if line.strip()]
    has_separator = any(is_separator_row(line) for line in cleaned)

    if not cleaned:
        return TableBlock(meta=meta, header=None, rows=[], block_index=block_index)

    if len(cleaned) >= 2 and is_separator_row(cleaned[1]):
        header = split_markdown_row(cleaned[0])
        data_lines = [line for line in cleaned[1:] if not is_separator_row(line)]
        rows = [split_markdown_row(line) for line in data_lines]
    else:
        header = None
        data_lines = [line for line in cleaned if not is_separator_row(line)]
        rows = [split_markdown_row(line) for line in data_lines]

    return TableBlock(
        meta=meta,
        header=header,
        rows=rows,
        has_separator=has_separator,
        block_index=block_index,
        raw_lines=list(lines),
    )


# --------------------------------------------------------------------------- #
# 页码锚定 Markdown → Page/Block
# --------------------------------------------------------------------------- #
def _parse_meta_payload(payload: str, default_doc: str, default_page: int) -> DocMeta:
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return DocMeta(doc_id=default_doc, page=default_page)
    return DocMeta(
        doc_id=str(obj.get("doc") or default_doc),
        page=int(obj.get("page") or default_page),
        section=obj.get("section") or None,
    )


def _is_heading_line(line: str) -> bool:
    return line.lstrip().startswith("#")


def _segment_block(lines: list[str]) -> list[tuple[list[str], str]]:
    """把一个块拆成有序片段：``text`` 段与 ``table`` 段。

    MinerU 正常情况下每个内容块自带一条 META，但真实数据里偶见「说明文字 + 表格」
    落在同一条 META 之下。若不拆分，整块会被判成文本，表格就此消失——这正是
    跨页缝合失效的隐蔽原因之一。
    """
    segments: list[tuple[list[str], str]] = []
    current: list[str] = []
    current_is_table: bool | None = None

    for line in lines:
        if not line.strip():
            continue
        is_table = looks_like_table_line(line)
        if current_is_table is None:
            current_is_table = is_table
        elif is_table != current_is_table:
            segments.append((current, "table" if current_is_table else "text"))
            current = []
            current_is_table = is_table
        current.append(line)

    if current and current_is_table is not None:
        segments.append((current, "table" if current_is_table else "text"))
    return segments


def parse_page_anchored_markdown(
    markdown: str,
    default_doc: str = "unknown",
) -> list[Page]:
    """解析页码锚定 Markdown，返回按页码升序排列的 :class:`Page` 列表。

    容错策略：
    - 文件开头若没有 META，则整段内容归入 ``default_doc`` 的第 1 页；
    - META 的 JSON 损坏时回退到默认坐标，不中断解析。
    """
    pages: dict[int, Page] = {}
    current_meta: DocMeta | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        if current_meta is None:
            return
        body = [line for line in current_lines if line.strip()]
        current_lines = []
        if not body:
            return
        page = pages.setdefault(current_meta.page, Page(number=current_meta.page))
        for segment, segment_kind in _segment_block(body):
            kind = segment_kind
            if kind == "text" and all(_is_heading_line(line) for line in segment):
                kind = "heading"
            block_index = len(page.blocks)
            table = (
                parse_markdown_table(segment, current_meta, block_index)
                if kind == "table"
                else None
            )
            page.blocks.append(
                Block(
                    meta=current_meta,
                    kind=kind,  # type: ignore[arg-type]
                    lines=segment,
                    block_index=block_index,
                    table=table,
                )
            )

    for line in markdown.splitlines():
        match = _META_RE.match(line)
        if match:
            flush()
            current_meta = _parse_meta_payload(
                match.group("payload"),
                default_doc=default_doc,
                default_page=current_meta.page if current_meta else 1,
            )
            continue
        if current_meta is None:
            # 文件开头没有 META：整段内容归入默认文档的第 1 页
            current_meta = DocMeta(doc_id=default_doc, page=1)
        current_lines.append(line)

    flush()
    return [pages[key] for key in sorted(pages)]


def load_page_anchored_markdown(path: str, default_doc: str = "unknown") -> list[Page]:
    """从文件读取并解析页码锚定 Markdown。"""
    with open(path, encoding="utf-8") as handle:
        return parse_page_anchored_markdown(handle.read(), default_doc=default_doc)


# --------------------------------------------------------------------------- #
# MinerU 的 table_body 是 HTML，需要先转成管道表才能进入缝合流程
# --------------------------------------------------------------------------- #
class _HTMLTableExtractor(HTMLParser):
    """把 HTML ``<table>`` 转成二维文本网格（忽略 rowspan/colspan）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row_has_th: list[bool] = []
        self._in_cell = False
        self._buffer: list[str] = []
        self._row: list[str] = []
        self._row_had_th = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
            self._row_had_th = False
        elif tag in ("td", "th"):
            self._in_cell = True
            self._buffer = []
            if tag == "th":
                self._row_had_th = True
        elif tag == "br" and self._in_cell:
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._row.append(" ".join("".join(self._buffer).split()))
            self._in_cell = False
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)
                self.row_has_th.append(self._row_had_th)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._buffer.append(data)


def html_table_to_rows(html: str) -> tuple[list[list[str]], bool]:
    """把 HTML 表格转成 ``(rows, first_row_is_header)``。

    ``first_row_is_header`` 依据首行是否使用 ``<th>`` 判断。这一信息很关键：
    MinerU 对**跨页续接表**输出的 HTML 没有 ``<th>``，若强行当成表头，下游缝合
    会把续接行误判成「新表自带表头」而拒绝拼接。
    """
    extractor = _HTMLTableExtractor()
    extractor.feed(html)
    extractor.close()
    if not extractor.rows:
        return [], False

    width = max(len(row) for row in extractor.rows)
    rows = [row + [""] * (width - len(row)) for row in extractor.rows]
    return rows, bool(extractor.row_has_th and extractor.row_has_th[0])


def html_table_to_markdown(html: str) -> str:
    """把 HTML 表格转成 Markdown 管道表。

    已知限制（Phase 0 接受）：``rowspan`` / ``colspan`` 不展开，合并单元格会被
    压平为独立单元格。年报表格经 MinerU 解析后通常已是规则网格，该限制主要影响
    少数带合并表头的报表；后续由 L1 抽取阶段结合 ``raw_text_matched`` 反查兜底。

    注意：Markdown 管道表在语法上必须有表头，本函数**总是**把首行当表头。
    需要保留「无表头」语义时请改用 :func:`html_table_to_rows`。
    """
    normalized, _ = html_table_to_rows(html)
    if not normalized:
        return ""

    width = len(normalized[0])
    lines = [
        "| " + " | ".join(normalized[0]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)
