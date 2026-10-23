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
    """把 HTML ``<table>`` 拆成「每行若干 ``(文本, rowspan, colspan)``」。

    **这一步不展开合并单元格**：``colspan`` 与 ``rowspan`` 都要靠列位置才能
    正确展开，而列位置只有把整张表放在一起看才知道——第 N 行必须跳过前面几行
    ``rowspan`` 占住的列。展开逻辑见 :func:`_expand_merged_cells`。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raw_rows: list[list[tuple[str, int, int]]] = []
        self.row_has_th: list[bool] = []
        self._in_cell = False
        self._buffer: list[str] = []
        self._row: list[tuple[str, int, int]] = []
        self._row_had_th = False
        self._cell_rowspan = 1
        self._cell_colspan = 1

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
            self._row_had_th = False
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
            self._cell_rowspan = max(rowspan, 1)
            self._cell_colspan = max(colspan, 1)
        elif tag == "br" and self._in_cell:
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._row.append(
                (
                    " ".join("".join(self._buffer).split()),
                    self._cell_rowspan,
                    self._cell_colspan,
                )
            )
            self._in_cell = False
        elif tag == "tr":
            if self._row:
                self.raw_rows.append(self._row)
                self.row_has_th.append(self._row_had_th)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._buffer.append(data)


def _expand_merged_cells(
    raw_rows: list[list[tuple[str, int, int]]],
) -> list[list[str]]:
    """按 ``rowspan`` / ``colspan`` 把稀疏行展开成等宽网格。

    ``colspan`` 展开是必须的：vlm 后端用 ``colspan="3"`` 表示「这一格横跨三列」
    （实测 P44 的「期末持有的其他证券投资」行），不展开会让后续数值整体左移。

    ``rowspan`` 展开同样必须：被 ``rowspan`` 覆盖的行自身没有这一格，若按顺序
    摆放，它后面的单元格会从第 0 列开始——实测 P13 的双层表头因此整体左移一列，
    第二层列名落到了第一列上。
    """
    grid: list[list[str]] = []
    pending: dict[tuple[int, int], str] = {}
    for row_index, cells in enumerate(raw_rows):
        row: list[str] = []
        queue = list(cells)
        column = 0
        while True:
            if (row_index, column) in pending:
                row.append(pending.pop((row_index, column)))
                column += 1
            elif queue:
                text, rowspan, colspan = queue.pop(0)
                row.append(text)
                row.extend([""] * (colspan - 1))
                for down in range(1, rowspan):
                    for across in range(colspan):
                        pending[(row_index + down, column + across)] = (
                            text if across == 0 else ""
                        )
                column += colspan
            else:
                break
        grid.append(row)
    return grid


_PURE_NUMBER_RE = re.compile(r"^[+-]?\(?[\d,]+(?:\.\d+)?\)?%?$")


def _is_pure_number(text: str) -> bool:
    """判断单元格是否为「纯数值」（容忍千分位、百分号、括号负数、--）。"""
    stripped = text.strip()
    if not stripped or stripped in {"--", "—", "-", "/"}:
        return False
    return bool(_PURE_NUMBER_RE.match(stripped))


#: 表头推断的向前探测行数。用于「期间表头 + 小计行」形态：
#: P137 首行是 ``['', '2025年', '2024年']``，紧邻的第二行却是分类标签
#: ``一、经营活动产生的现金流量:``（纯文本、无数值）。只看第二行会漏判，
#: 必须向后看几行才能确认「这是一张以期间为列的表」。
HEADER_LOOKAHEAD_ROWS = 3


def html_table_to_rows(
    html: str,
) -> tuple[list[list[str]], bool, list[tuple[int, str]]]:
    """把 HTML 表格转成 ``(rows, first_row_is_header, section_rows)``。

    ``rows`` 是**已展开合并单元格**的等宽网格：``colspan`` 补空占位，``rowspan``
    把首格内容下发给被覆盖的每一行（见 :func:`_expand_merged_cells`）。

    ``first_row_is_header`` 判定依次尝试三条判据：

    1. 首行使用 ``<th>``；
    2. 首行单元格全部带 ``rowspan≥2``（MinerU 的合并表头）；
    3. **纯数值分布推断**（vlm 后端无任何表头标记）：
       首行没有纯数值 + 首行至少 2 个非空单元格 + 其后 ``HEADER_LOOKAHEAD_ROWS``
       行内至少有一行含纯数值。

    ``section_rows`` 是「全宽合并行」清单 ``[(数据行下标, 文本), ...]``。
    这些行在展开 ``colspan`` 后会退化成「首列有值、其余全空」的形态，
    单看数据无法与「恰好多列为空的数据行」区分，因此**必须在解析阶段显式记录**。
    它们是物理表内部的潜在分节点（嵌入式小标题 / 子栏标注 / 行内值），
    交给 :mod:`finkb.parser.section_splitter` 做语义归类。

    全空行会被剔除（``rowspan`` 未展开时的垃圾行）。
    """
    extractor = _HTMLTableExtractor()
    extractor.feed(html)
    extractor.close()
    if not extractor.raw_rows:
        return [], False, []

    # 自身一格内容都没有的行，是 MinerU 给 ``rowspan`` 补的占位行（坑-13），必须剔除。
    # 判据必须是「本行自有单元格是否全空」，不能是「展开后是否全空」——
    # 展开后这些行会被上面 rowspan 的进位内容填满，看起来像真实数据行。
    keep = [
        index
        for index, cells in enumerate(extractor.raw_rows)
        if any(text.strip() for text, _, _ in cells)
    ]
    if not keep:
        return [], False, []

    grid = _expand_merged_cells(extractor.raw_rows)
    kept_rows = [grid[index] for index in keep]
    kept_th = [extractor.row_has_th[index] for index in keep]
    kept_raw = [extractor.raw_rows[index] for index in keep]

    # rowspan / colspan 展开后行长度已经等于逻辑列数，直接取最大长度即可
    width = max(len(row) for row in kept_rows)
    rows = [row + [""] * (width - len(row)) for row in kept_rows]

    first_is_header = bool(kept_th and kept_th[0])
    if not first_is_header and kept_raw:
        first_spans = [rowspan for _, rowspan, _ in kept_raw[0]]
        # MinerU 的合并表头：首行所有单元格 rowspan≥2（实测 P43 证券投资表）
        if len(first_spans) >= 2 and all(span >= 2 for span in first_spans):
            first_is_header = True

    # 纯数值分布推断（vlm 后端）。
    # 三条护栏缺一不可：
    #   ① 首行至少 2 个非空单元格 —— 全宽单格是「表标题」而不是表头（实测 P89）；
    #   ② 首行没有纯数值 —— 有数值说明是数据行；
    #   ③ 向后看 HEADER_LOOKAHEAD_ROWS 行内有数值 —— 兜住「期间表头 + 小计行」形态。
    if not first_is_header and len(rows) >= 2:
        first_non_empty = sum(1 for cell in rows[0] if cell.strip())
        first_numeric = sum(1 for cell in rows[0] if _is_pure_number(cell))
        lookahead_numeric = any(
            _is_pure_number(cell)
            for row in rows[1 : 1 + HEADER_LOOKAHEAD_ROWS]
            for cell in row
        )
        if first_non_empty >= 2 and first_numeric == 0 and lookahead_numeric:
            first_is_header = True

    # 纯文本表兜底：整表没有任何数值（如「关键审计事项」表）时，上一条判据失效。
    # 此时若首行所有非空单元格都以冒号结尾，按列名处理。
    # 实测 P118：首行 ['关键审计事项:', '该事项在审计中是如何应对:'] → 表头。
    if not first_is_header and len(rows) >= 2:
        first_cells = [cell.strip() for cell in rows[0] if cell.strip()]
        if len(first_cells) >= 2 and all(
            cell.endswith((":", "：")) for cell in first_cells
        ):
            first_is_header = True

    # 记录「全宽合并行」：原始 HTML 里只有一个单元格且跨满整行。
    # 下标是相对「数据行」（不含表头）的位置，与 TableBlock.rows 对齐。
    section_rows: list[tuple[int, str]] = []
    header_offset = 1 if first_is_header else 0
    for pos, cells in enumerate(kept_raw):
        data_index = pos - header_offset
        if data_index < 0:
            continue
        # 全宽合并行：原始 HTML 里只有一个单元格且跨满整行
        if len(cells) == 1 and cells[0][2] == width:
            text = cells[0][0].strip()
            if text:
                section_rows.append((data_index, text))

    return rows, first_is_header, section_rows


def html_table_to_markdown(html: str) -> str:
    """把 HTML 表格转成 Markdown 管道表。

    已知限制（Phase 0 接受）：``rowspan`` / ``colspan`` 不展开，合并单元格会被
    压平为独立单元格。年报表格经 MinerU 解析后通常已是规则网格，该限制主要影响
    少数带合并表头的报表；后续由 L1 抽取阶段结合 ``raw_text_matched`` 反查兜底。

    注意：Markdown 管道表在语法上必须有表头，本函数**总是**把首行当表头。
    需要保留「无表头」语义时请改用 :func:`html_table_to_rows`。
    """
    normalized, _, _ = html_table_to_rows(html)
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
