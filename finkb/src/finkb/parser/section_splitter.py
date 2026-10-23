"""物理表 → 逻辑表切分（步骤 2）。

问题
----
一个 MinerU 的 ``<table>``（可能跨页，经缝合后成为 :class:`StitchedTable`）
内部可以包含多个**逻辑数据表**，分节点是表格体内的**全宽合并行**
（原始 HTML 里 ``<td colspan="N">``）。实测比亚迪 2025 年报 P89–P91：

    一个 11 列 85 行的物理表
    ├─ 「公司及其子公司对外担保情况(不包括对子公司的担保)」+ 11 列表头 + 数据
    ├─ 「公司对子公司的担保情况」                      + 11 列表头 + 数据
    ├─ 「子公司对子公司的担保情况」                    + 11 列表头 + 数据   ← 用户人工发现的那处
    ├─ 「公司担保总额(即前三大项的合计)」              + 键值对（汇总块）
    └─ 「其中:」                                        + 键值对

三种语义，判据不同
------------------
============  ==========================================  ================
分类          特征                                        处理
============  ==========================================  ================
``new_table`` 后继行看起来是表头（≥3 非空、无纯数值）      该段自有表头
``summary``   ≥4 列表，且后继行非空单元格 ≤4（键值对）      汇总块
``heading``   窄表，文本以冒号结尾或带中文序号              分节标签
``footnote``  以「注」开头或长度 > 40                       脚注，**不切分**
============  ==========================================  ================

核心设计：**表头继承**
--------------------
切分**不产生无表头的子表**。没有自有表头的段继承父表表头
（``header_source="inherited"``）。这是让 P137 现金流量表在切开后仍能保住
「2025年 / 2024年」期间语义的关键——否则下游 L1 拿到的是「一堆没有列名的数字」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from finkb.common.models import LogicalTable, SectionKind, StitchedTable

#: 中文序号 / 括号序号 / 「第N」开头 —— 分节标题的典型标志
_HEADING_PREFIX_RE = re.compile(
    r"^(?:[一二三四五六七八九十]+[、.]|[（(]\s*\d+\s*[)）]|第[一二三四五六七八九十]+[、.章节])"
)


@dataclass(frozen=True)
class SplitOptions:
    """切分策略。

    Attributes:
        header_similarity_threshold: 后继行与父表表头判定为「同一表头」的相似度下限。
        footnote_prefix: 脚注行前缀（命中即不切分）。
        footnote_min_length: 超过该长度视为脚注（实测 P109 的「注1:…」长达 200+ 字）。
        summary_max_nonempty: 判定为汇总块时，后继行允许的最大非空单元格数。
        summary_max_rows: 判定为汇总块时，段内允许的最大行数。
            只看「非空格数」会漏判空列多的宽表：财务报表附注的
            「附注七」列大量留空，10–21 行的数据表每行也只有 3 个非空格，
            会被误判成汇总块（实测 7 张核心报表中招）。**行数**才是有区分度的维度：
            真汇总块 0–3 行，误判的 ≥8 行。
        summary_min_width: 只在 **≥ 该列数** 的表上启用汇总块判据。
            窄表（2–3 列）里任何行都满足「非空 ≤4」，该判据恒真、没有区分力。
        narrow_heading_only: 窄表是否只按「冒号/中文序号」切分（保守模式）。
    """

    header_similarity_threshold: float = 0.5
    footnote_prefix: str = "注"
    footnote_min_length: int = 40
    summary_max_nonempty: int = 4
    summary_max_rows: int = 3
    summary_min_width: int = 4
    narrow_heading_only: bool = True


@dataclass
class SectionCut:
    """一个分节点的判定结果（审计用）。"""

    row: int
    text: str
    kind: SectionKind
    own_header: bool
    reason: str


@dataclass
class SplitResult:
    """切分产物。"""

    tables: list[LogicalTable] = field(default_factory=list)
    cuts: list[SectionCut] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.tables)


def _non_empty(row: list[str]) -> list[str]:
    return [cell for cell in row if cell.strip()]


def _is_pure_number(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped in {"--", "—", "-", "/"}:
        return False
    return bool(re.fullmatch(r"[+-]?\(?[\d,]+(?:\.\d+)?\)?%?", stripped))


def _numeric_count(row: list[str]) -> int:
    return sum(1 for cell in row if _is_pure_number(cell))


def _header_similarity(candidate: list[str], reference: list[str] | None) -> float:
    """两行作为表头的相似度（非空文本的 Jaccard）。"""
    if reference is None:
        return 0.0
    a = {cell.strip() for cell in candidate if cell.strip()}
    b = {cell.strip() for cell in reference if cell.strip()}
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cell_kind(cell: str) -> str:
    """单元格语义：``key`` 纯文本 / ``value`` 含数字 / ``empty`` 空。"""
    text = cell.strip()
    if not text:
        return "empty"
    if _is_pure_number(text):
        return "value"
    return "value" if any(ch.isdigit() for ch in text) else "key"


def parse_kv_row(row: list[str]) -> tuple[list[tuple[str, str]], list[str]] | None:
    """尝试把「单行横表」解析成键值对。

    单行横表：同一行里并排多组键值对，例如三季报 P4_T06 的首行
    ``报告期末普通股股东总数 | 642,600 | 报告期末表决权恢复的优先股股东总数 | 0``。
    它不是「一行数据」，因此不能按列取值。

    两种形态：

    ① ``键|值|键|值`` —— 全部配对；
    ② ``值|键|值|键|…|值`` —— 首格是**孤儿值**（实测 P109：首格标签在 MinerU
       输出里丢失，HTML 里是空 ``<td>``），剥离后其余恰好配对。

    **完整配对数必须 ≥2**：只有 1 组配对的 3 格行（如
    ``房屋及建筑物 | 41,744,687 | 尚未办完所有手续``）是普通数据行，不是横表。

    Returns:
        ``(pairs, orphan_values)``；不是横表时返回 ``None``。
    """
    cells = [cell.strip() for cell in row if cell.strip()]
    if len(cells) < 4:
        return None
    kinds = [_cell_kind(cell) for cell in cells]

    # ① 键|值|键|值 …
    if all(kind == ("key" if i % 2 == 0 else "value") for i, kind in enumerate(kinds)):
        pairs = list(zip(cells[0::2], cells[1::2]))
        return (pairs, []) if len(pairs) >= 2 else None

    # ② 首格孤儿值 + 键|值|…
    if kinds[0] == "value" and all(
        kind == ("value" if i % 2 == 0 else "key") for i, kind in enumerate(kinds)
    ):
        pairs = list(zip(cells[1::2], cells[2::2]))
        return (pairs, [cells[0]]) if len(pairs) >= 2 else None

    return None


def _looks_like_heading(text: str) -> bool:
    stripped = text.strip()
    return stripped.endswith((":", "：")) or bool(_HEADING_PREFIX_RE.match(stripped))


def classify_cut(
    row_index: int,
    text: str,
    rows: list[list[str]],
    parent_header: list[str] | None,
    width: int,
    options: SplitOptions,
) -> SectionCut:
    """对一个全宽行给出语义分类。"""
    stripped = text.strip()

    if stripped.startswith(options.footnote_prefix) or len(stripped) > options.footnote_min_length:
        return SectionCut(row_index, text, "footnote", False, f"脚注（长度 {len(stripped)}）")

    nxt = rows[row_index + 1] if row_index + 1 < len(rows) else None
    if nxt is None:
        return SectionCut(row_index, text, "data", False, "末行，无后继")

    next_non_empty = _non_empty(nxt)
    next_numeric = _numeric_count(nxt)

    # ① 后继行像表头：列名充足且无纯数值
    if len(next_non_empty) >= 3 and next_numeric == 0:
        similarity = _header_similarity(nxt, parent_header)
        if parent_header is None or similarity >= options.header_similarity_threshold:
            return SectionCut(
                row_index,
                text,
                "data",
                True,
                f"后继行是表头（相似度 {similarity:.0%}）",
            )

    # ② 宽表上的键值对汇总块（窄表禁用：判据在那里恒真）
    if width >= options.summary_min_width and len(next_non_empty) <= options.summary_max_nonempty:
        return SectionCut(
            row_index,
            text,
            "summary",
            False,
            f"后继为键值对（{len(next_non_empty)} 个非空格，表宽 {width}）",
        )

    # ③ 窄表：只认显式标题标记，避免把数据行误当分节
    if width < options.summary_min_width:
        if options.narrow_heading_only and not _looks_like_heading(stripped):
            return SectionCut(row_index, text, "data", False, "窄表且无标题标记，保留为数据行")
        return SectionCut(row_index, text, "data", False, "窄表分节标题")

    return SectionCut(row_index, text, "data", False, "后继为数据行，按分节标签处理")


def _section_kind(
    own_header: bool,
    cut_kind: SectionKind,
    rows: list[list[str]],
    parent_header: list[str] | None = None,
    options: SplitOptions | None = None,
) -> SectionKind:
    """结合段内内容细化类型。

    ``kv_row`` 的守卫是**结构事实**而非内容猜测：父表已经有表头时，
    该表的每一行都是「按列取值」的数据行，不可能再是并排的键值对。

    早期版本只看「单元格是否含数字」来判断键/值，实测会出错——
    实测 P186_T134：``房屋及建筑物 | 41,744,687 | 尚未办完所有手续``
    三格全是**值**（项目/账面价值/未办妥原因），但「尚未办完所有手续」
    不含数字，会被误判成「键」，从而凑成 ``键|值|键`` 的假交替。
    """
    if cut_kind == "summary" and not own_header and _is_short_summary(rows, options):
        return "summary"
    if (
        not own_header
        and parent_header is None
        and len(rows) == 1
        and parse_kv_row(rows[0]) is not None
    ):
        return "kv_row"
    if not own_header and len(rows) <= 2 and all(_numeric_count(row) == 0 for row in rows):
        # 纯文本的短段 —— 典型是「关键审计事项」这类标签 + 长文
        return "kv"
    return "data"


def _is_short_summary(rows: list[list[str]], options: SplitOptions | None) -> bool:
    """汇总块必须又短又稀。

    「稀」是切分时的必要条件（``summary_max_nonempty``），「短」是分段后
    才能确认的充分条件——两者都满足才认。只判「稀」会把空列多的宽表
    整张吞成 summary（实测 P130_T77 现金流量表 12/15/21 行）。
    """
    opts = options or SplitOptions()
    if len(rows) > opts.summary_max_rows:
        return False
    return all(len(_non_empty(row)) <= opts.summary_max_nonempty for row in rows)


def _collect_cut_candidates(
    table: StitchedTable,
    rows: list[list[str]],
) -> list[tuple[int, str]]:
    """汇总两类分节候选。

    1. **colspan 全宽行** —— 解析阶段已记录（``table.section_rows``），是最可靠的信号；
    2. **「仅首列非空 + 标题形态」行** —— 补充判据。

    为什么需要第 2 类：MinerU 对**同一语义**的分节标题会给出两种编码。实测 P137
    现金流量表里，「一、经营活动…」「二、投资活动…」是 3 个普通 ``<td>``（后两格空），
    而「三、筹资活动…」是 ``<td colspan="3">``。只认 colspan 会漏掉前两个。

    第 2 类要求「标题形态」（冒号结尾或中文序号），因此不会把普通的稀疏数据行
    误判为分节——实测新增的 20 处全部是真标题。
    """
    declared = [(index, text) for index, text in table.section_rows if 0 <= index < len(rows)]
    known = {index for index, _ in declared}

    extra: list[tuple[int, str]] = []
    for index, row in enumerate(rows):
        if index in known:
            continue
        non_empty = _non_empty(row)
        if len(non_empty) != 1:
            continue
        head = row[0].strip()
        if head != non_empty[0]:
            continue
        if _looks_like_heading(head):
            extra.append((index, head))

    return sorted(declared + extra)


def split_sections(
    table: StitchedTable,
    options: SplitOptions | None = None,
) -> SplitResult:
    """把一个物理表按内部全宽行切分成逻辑表列表。

    始终至少返回 1 张逻辑表（无分节点时就是整张表）。
    """
    opts = options or SplitOptions()
    rows = table.rows
    width = max((len(row) for row in rows), default=0) or table.ncols
    parent_header = table.header

    result = SplitResult()

    if not rows:
        result.tables.append(_build_one(table, table.header, "inherited", [], [], (0, 0), "data", 1))
        return result

    cuts: list[SectionCut] = []
    for index, text in _collect_cut_candidates(table, rows):
        if not (0 <= index < len(rows)):
            continue
        cut = classify_cut(index, text, rows, parent_header, width, opts)
        result.cuts.append(cut)
        if cut.kind != "footnote":
            cuts.append(cut)

    seq = 0

    def emit(start: int, end: int, path: list[str], header: list[str] | None,
             header_source: str, cut: SectionCut | None) -> None:
        nonlocal seq
        segment = rows[start:end]
        if not segment:
            return
        cut_kind: SectionKind = cut.kind if cut is not None else "data"
        kind = _section_kind(
            header_source == "self", cut_kind, segment, header, opts
        )
        if cut is not None and cut_kind == "summary" and kind != "summary":
            # 切分时只看到「后继行稀」，分段后才发现它并不短 —— 改判并留痕
            cut.kind = "data"
            cut.reason += f"；但段内 {len(segment)} 行，降级为 data"
        seq += 1
        result.tables.append(
            _finalize(LogicalTable(
                logical_id=f"{table.parent_id}#S{seq:02d}",
                parent_id=table.parent_id,
                header=header,
                header_source=header_source,  # type: ignore[arg-type]
                section_path=list(path),
                rows=segment,
                page_start=table.page_start,
                page_end=table.page_end,
                row_range=(start, end),
                kind=kind,
            ))
        )

    # 首个分节点若在第 0 行，它是整张表的标题
    cursor = 0
    path: list[str] = []
    current_header = parent_header
    current_source = "inherited"
    pending_cut: SectionCut | None = None

    if cuts and cuts[0].row == 0:
        first = cuts[0]
        path = [first.text]
        if first.own_header:
            current_header = rows[1]
            current_source = "self"
            cursor = 2
        else:
            current_source = "inherited"
            cursor = 1
        pending_cut = first
        cuts = cuts[1:]

    for cut in cuts:
        emit(cursor, cut.row, path, current_header, current_source, pending_cut)
        path = [cut.text]
        if cut.own_header and cut.row + 1 < len(rows):
            current_header = rows[cut.row + 1]
            current_source = "self"
            cursor = cut.row + 2
        else:
            current_header = parent_header
            current_source = "inherited"
            cursor = cut.row + 1
        pending_cut = cut

    emit(cursor, len(rows), path, current_header, current_source, pending_cut)

    if not result.tables:
        result.tables.append(
            _build_one(table, parent_header, "inherited", [], rows, (0, len(rows)), "data", 1)
        )
    return result


def _finalize(logical: LogicalTable) -> LogicalTable:
    """为 ``kv_row`` 填充 ``pairs`` / ``orphan_values``。"""
    if logical.kind != "kv_row" or len(logical.rows) != 1:
        return logical
    parsed = parse_kv_row(logical.rows[0])
    if parsed is not None:
        logical.pairs, logical.orphan_values = parsed
    return logical


def _build_one(
    table: StitchedTable,
    header: list[str] | None,
    header_source: str,
    path: list[str],
    rows: list[list[str]],
    row_range: tuple[int, int],
    kind: SectionKind,
    seq: int,
) -> LogicalTable:
    return _finalize(LogicalTable(
        logical_id=f"{table.parent_id}#S{seq:02d}",
        parent_id=table.parent_id,
        header=header,
        header_source=header_source,  # type: ignore[arg-type]
        section_path=list(path),
        rows=[list(row) for row in rows],
        page_start=table.page_start,
        page_end=table.page_end,
        row_range=row_range,
        kind=kind,
        # 只有继承父表表头的段才拿到父表的多层表头原貌；段自带表头时
        # 那一行已经被切进 rows（切分点后一行），层数信息在这里没有意义。
        header_rows=[list(row) for row in table.header_rows]
        if header_source == "inherited"
        else [],
        table_title=table.table_title if header_source == "inherited" else None,
    ))


def split_all(
    tables: list[StitchedTable],
    options: SplitOptions | None = None,
) -> list[LogicalTable]:
    """对一批物理表执行切分，返回扁平的逻辑表列表。"""
    out: list[LogicalTable] = []
    for table in tables:
        out.extend(split_sections(table, options).tables)
    return out
