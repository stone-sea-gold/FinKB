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
    """把 HTML ``<table>`` 转成二维文本网格。

    **``colspan`` 会被展开**（占位补空字符串），``rowspan`` 只记录不展开。
    展开 ``colspan`` 是必须的：vlm 后端用 ``colspan="3"`` 表示「这一格横跨三列」
    （实测 P44 的「期末持有的其他证券投资」行）。若不展开，后续数值会整体左移，
    缝合后的表格列就错位了——这比「缝不上」更危险，因为它会静默产出错误数据。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row_has_th: list[bool] = []
        self.row_spans: list[list[int]] = []
        self._in_cell = False
        self._buffer: list[str] = []
        self._row: list[str] = []
        self._row_had_th = False
        self._cell_spans: list[int] = []
        self._cell_colspan = 1

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
            self._row_had_th = False
            self._cell_spans = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._buffer = []
            if tag == "th":
                self._row_had_th = True
            rowspan, colspan = 1, 1
            for name, value in attrs:
                if not value:
                    continue
                try:
                    parsed = int(str(value).strip().strip('"'))
                except ValueError:
                    continue
                if name == "rowspan":
                    rowspan = parsed
                elif name == "colspan":
                    colspan = parsed
            self._cell_spans.append(max(rowspan, 1))
            self._cell_colspan = max(colspan, 1)
        elif tag == "br" and self._in_cell:
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._row.append(" ".join("".join(self._buffer).split()))
            # 展开 colspan：被跨越的列补空字符串，保持后续单元格列位不变
            self._row.extend([""] * (self._cell_colspan - 1))
            self._in_cell = False
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)
                self.row_has_th.append(self._row_had_th)
                self.row_spans.append(self._cell_spans)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._buffer.append(data)


_PURE_NUMBER_RE = re.compile(r"^[+-]?\(?[\d,]+(?:\.\d+)?\)?%?$")


def _is_pure_number(text: str) -> bool:
    """判断单元格是否为「纯数值」（容忍千分位、百分号、括号负数、--）。"""
    stripped = text.strip()
    if not stripped or stripped in {"--", "—", "-", "/"}:
        return False
    return bool(_PURE_NUMBER_RE.match(stripped))


def html_table_to_rows(html: str) -> tuple[list[list[str]], bool]:
    """把 HTML 表格转成 ``(rows, first_row_is_header)``。

    ``first_row_is_header`` 依据首行是否使用 ``<th>`` 判断。

    **实测修正**：MinerU 的表头**不用 ``<th>``**，而是 ``<td rowspan="2">``。
    因此这里额外采用一条兼容判据——首行单元格全部带 ``rowspan≥2`` 时，
    也认定它是表头（合并表头行）。

    **全空行会被剔除**：``rowspan`` 未被展开时，后续行会退化成「1 个空单元格」的
    垃圾行（实测 P43 证券投资表就有一条）。这类行若保留，会在缝合时污染数据，
    也会让 L1 抽到空记录。
    """
    extractor = _HTMLTableExtractor()
    extractor.feed(html)
    extractor.close()
    if not extractor.rows:
        return [], False

    keep = [
        index
        for index, row in enumerate(extractor.rows)
        if any(cell.strip() for cell in row)
    ]
    if not keep:
        return [], False

    kept_rows = [extractor.rows[index] for index in keep]
    kept_th = [extractor.row_has_th[index] for index in keep]
    kept_spans = [extractor.row_spans[index] for index in keep]

    width = max(len(row) for row in kept_rows)
    rows = [row + [""] * (width - len(row)) for row in kept_rows]

    first_is_header = bool(kept_th and kept_th[0])
    if not first_is_header and kept_spans:
        first_spans = kept_spans[0]
        # MinerU 的合并表头：首行所有单元格 rowspan≥2（实测 P43 证券投资表）
        if len(first_spans) >= 2 and all(span >= 2 for span in first_spans):
            first_is_header = True

    # 兜底推断：vlm 后端对表头**不做任何标记**（全是普通 <td>，无 th 无 rowspan）。
    # 用一条保守的统计判据：首行没有任何「纯数值」单元格，而第二行至少有一个。
    # 实测校验：P43 表首行是 14 个中文列名（0 个纯数值）→ 判为表头；
    #           P44 续接表首行是「债券/019742/113,357,000.00…」（多个纯数值）→ 不判表头。
    # 注意「2025年」这类带数字但非纯数值的表头不会误伤本判据。
    if not first_is_header and len(rows) >= 2:
        first_numeric = sum(1 for cell in rows[0] if _is_pure_number(cell))
        second_numeric = sum(1 for cell in rows[1] if _is_pure_number(cell))
        if first_numeric == 0 and second_numeric >= 1:
            first_is_header = True

    return rows, first_is_header


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
