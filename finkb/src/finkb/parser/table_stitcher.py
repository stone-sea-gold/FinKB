"""跨页表格缝合（FinKB Phase 0 的核心原创算法）。

问题背景
--------
A 股年报中的合并报表、分产品收入表、现金流量表常跨页。MinerU 会按页切分，
导致第 N+1 页的表格变成「无表头的孤儿数据行」——列名丢失后，任何下游抽取
都无法判断 ``| 25.10 | 23.02 |`` 到底是毛利率还是销量。

缝合判据（方案 §7.1）
--------------------
当且仅当同时满足下列条件时，判定第 N+1 页首块表格是第 N 页末块表格的续接：

1. 第 N 页的表格是该页的**最后一个内容块**；
2. 第 N+1 页的表格是该页的**第一个内容块**；
3. 两表**列数一致**；
4. 第 N+1 页的表格**没有表头**（首行即数据行），或其表头与第 N 页**完全重复**。

条件 4 是防误缝的关键：年报中正常起始的表格几乎总带表头，无表头的页首表格
绝大多数是上一页的延续。针对「MinerU 重复输出表头」的变体，本模块会丢弃重复
表头后只拼数据行，并在审计记录中标注。

**「内容块」指非噪音块。** 年报每页页首都有一行运行页眉（``公司名 + 年份 +
页码``），若它被解析成独立块，条件 2 会直接失效、整张跨页表缝不上。因此本模块
通过 ``Block.is_noise`` 跳过已打标的噪音块——这使缝合不依赖噪音清洗是否彻底，
是比「先洗干净再缝」更稳的组合。

三页及以上链式拆分通过循环续接处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Sequence

from finkb.common.models import (
    Page,
    StitchRecord,
    StitchedTable,
    StitchResult,
    TableBlock,
)

_NUMERIC_RE = re.compile(r"^[+-]?\(?[\d,]+(?:\.\d+)?\)?%?$")

#: 财务表的「表结束」标志词。出现在最后一行首列即认定该表已完整。
#: 实测依据：P65 董监高薪酬表、P214 所得税费用表、P229 合计表、P132 资产负债表
#: 均以合计/总计行收尾，之后的内容都是**新表**，不该尝试续接。
_TOTAL_MARKERS: tuple[str, ...] = (
    "合计",
    "总计",
    "小计",
    "累计",
    "总 计",
    "合 计",
    "小 计",
)

#: ``_merge_verdict`` 的三种结论。
#: - ``merge``：确认续接，执行拼接
#: - ``new``：确认是另一张表（如自带表头），**静默放弃**，不产生告警
#: - ``reject``：无法确定，记录为待人工核对的拒绝项
Verdict = Literal["merge", "new", "reject"]


@dataclass(frozen=True)
class StitchOptions:
    """缝合策略开关。

    Attributes:
        require_equal_columns: 是否要求列数严格一致。默认 ``True``——
            列数不同几乎必然不是同一张表，是最强的防误缝约束。
        allow_duplicate_header: 是否接受「下一页重复输出表头」的变体。
            开启后重复表头会被丢弃，只拼数据行。
        require_numeric_first_row: 是否要求续接表首行至少含一个数值单元格。
            默认关闭：财报续接行的首列常是行标签（如「其中：…」），强制数值
            会漏缝。仅在能确认数据源格式干净时开启。
        stop_on_total_row: 起始表末行是「合计/总计…」时直接判定表已完整，
            不再尝试续接。**不会产生拒绝告警**——这是正常情况，不是异常。
        total_row_markers: 表结束标志词，按「首列包含」匹配。
    """

    require_equal_columns: bool = True
    allow_duplicate_header: bool = True
    require_numeric_first_row: bool = False
    stop_on_total_row: bool = True
    total_row_markers: tuple[str, ...] = _TOTAL_MARKERS


def is_numeric_like(cell: str) -> bool:
    """判断单元格是否像一个数值（容忍千分位、百分号、括号负数）。"""
    text = cell.strip()
    if not text or text in {"-", "—", "－", "/"}:
        return False
    return bool(_NUMERIC_RE.match(text))


def is_total_row(row: list[str], markers: tuple[str, ...]) -> bool:
    """判断某行是否为合计/总计/小计行（只看首列）。"""
    if not row:
        return False
    first = row[0].strip()
    if not first:
        return False
    return any(marker in first for marker in markers)


def table_ends_with_total(table: StitchedTable, options: StitchOptions) -> bool:
    """起始表末行是否为合计行 —— 是则说明该表已完整，无需续接。"""
    if not options.stop_on_total_row or not table.rows:
        return False
    return is_total_row(table.rows[-1], options.total_row_markers)


def _table_ids(doc_id: str, page_start: int, page_end: int, seq: int) -> tuple[str, str]:
    table_id = f"{doc_id}#P{page_start}_T{seq:02d}"
    if page_start == page_end:
        parent_id = table_id
    else:
        parent_id = f"{doc_id}#P{page_start}-P{page_end}_T{seq:02d}"
    return table_id, parent_id


def first_content_index(page: Page) -> int | None:
    """返回该页第一个**非噪音**块的位置；整页皆噪音时返回 ``None``。"""
    for index, block in enumerate(page.blocks):
        if not block.is_noise:
            return index
    return None


def last_content_index(page: Page) -> int | None:
    """返回该页最后一个**非噪音**块的位置；整页皆噪音时返回 ``None``。"""
    for index in range(len(page.blocks) - 1, -1, -1):
        if not page.blocks[index].is_noise:
            return index
    return None


def _merge_verdict(
    head: StitchedTable,
    tail: TableBlock,
    options: StitchOptions,
) -> tuple[Verdict, str]:
    """判断 ``tail`` 是否为 ``head`` 的续接，返回 (结论, 理由)。

    判据顺序（P0 修订）：**先看表头性质，再比列数**。
    早期版本先比列数，导致「续接表自带表头」被误报成「列数不一致」，
    把排查方向引偏了。自带表头是**确定性的新表信号**，优先级最高。
    """
    if not tail.rows:
        return "new", "续接表没有数据行"

    if tail.header is not None:
        if options.allow_duplicate_header and tail.header == head.header:
            return "merge", "重复表头，丢弃后拼接"
        return "new", "续接表带独立表头，判定为新表"

    if options.require_equal_columns and tail.ncols != head.ncols:
        return "reject", f"列数不一致（起始表 {head.ncols} 列 / 续接表 {tail.ncols} 列）"

    if options.require_numeric_first_row and not any(
        is_numeric_like(cell) for cell in tail.rows[0]
    ):
        return "reject", "续接表首行无数值单元格"

    return "merge", "无表头续接"


def _can_merge(
    head: StitchedTable,
    tail: TableBlock,
    options: StitchOptions,
) -> tuple[bool, str]:
    """兼容旧接口：仅返回是否缝合与理由。"""
    verdict, reason = _merge_verdict(head, tail, options)
    return verdict == "merge", reason


def stitch_tables(
    pages: Sequence[Page],
    options: StitchOptions | None = None,
) -> StitchResult:
    """对按页组织的块序列执行跨页表格缝合。

    Args:
        pages: 按页码升序的页面列表（``parse_page_anchored_markdown`` 的输出）。
        options: 缝合策略，默认使用 :class:`StitchOptions` 的保守配置。

    Returns:
        :class:`StitchResult`，包含全部逻辑表与审计记录。未发生缝合的表格也会
        被返回，``source_tables == 1``。
    """
    opts = options or StitchOptions()
    result = StitchResult()
    consumed: set[tuple[int, int]] = set()
    seq = 0

    for page_pos, page in enumerate(pages):
        for block_pos, block in enumerate(page.blocks):
            if block.kind != "table" or block.table is None or block.is_noise:
                continue
            if (page_pos, block_pos) in consumed:
                continue

            first = block.table
            seq += 1
            table_id, parent_id = _table_ids(
                first.meta.doc_id, first.page, first.page, seq
            )
            stitched = StitchedTable(
                doc_id=first.meta.doc_id,
                header=list(first.header) if first.header is not None else None,
                rows=[list(row) for row in first.rows],
                page_start=first.page,
                page_end=first.last_page,
                source_tables=1,
                parent_id=parent_id,
                table_id=table_id,
                section=first.meta.section,
                section_rows=list(first.section_rows),
                header_rows=[list(row) for row in first.header_rows],
                table_title=first.table_title,
                period_label=first.period_label,
            )

            pages_touched = [first.page]
            reasons: list[str] = []
            cursor_page = page_pos
            cursor_block = block_pos

            while True:
                current_page = pages[cursor_page]
                if cursor_block != last_content_index(current_page):
                    break  # 当前表之后还有正文，页没被截断
                next_pos = cursor_page + 1
                if next_pos >= len(pages):
                    break
                next_page = pages[next_pos]
                first_index = first_content_index(next_page)
                if first_index is None:
                    break
                next_block = next_page.blocks[first_index]
                if next_block.kind != "table" or next_block.table is None:
                    break
                if (next_pos, first_index) in consumed:
                    break

                # P1：起始表以「合计/总计」收尾 → 表已完整，静默结束。
                # 这是正常情况，不记拒绝——记了只会制造噪声，掩盖真正的异常。
                if table_ends_with_total(stitched, opts):
                    break

                verdict, reason = _merge_verdict(stitched, next_block.table, opts)
                if verdict == "new":
                    # 确认是另一张表（例如自带表头），正常情况，静默结束。
                    break
                if verdict == "reject":
                    # 无法确定是否为续接（典型：列数不符），留待人工核对。
                    result.rejections.append(
                        StitchRecord(
                            doc_id=first.meta.doc_id,
                            pages=[stitched.page_start, next_block.table.page],
                            ncols=next_block.table.ncols,
                            reason=reason,
                        )
                    )
                    break

                offset = len(stitched.rows)
                stitched.rows.extend(list(row) for row in next_block.table.rows)
                stitched.section_rows.extend(
                    (index + offset, text) for index, text in next_block.table.section_rows
                )
                stitched.page_end = next_block.table.last_page
                stitched.source_tables += 1
                pages_touched.append(next_block.table.page)
                reasons.append(reason)
                consumed.add((next_pos, first_index))

                cursor_page = next_pos
                cursor_block = first_index

            table_id, parent_id = _table_ids(
                first.meta.doc_id, stitched.page_start, stitched.page_end, seq
            )
            stitched.table_id = table_id
            stitched.parent_id = parent_id

            if stitched.is_stitched:
                result.records.append(
                    StitchRecord(
                        doc_id=first.meta.doc_id,
                        pages=list(pages_touched),
                        ncols=stitched.ncols,
                        reason="; ".join(reasons),
                    )
                )
            result.tables.append(stitched)

    return result
