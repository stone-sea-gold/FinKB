"""FinKB 核心数据模型。

本模块定义 L0 凭证层的最小中间表示（IR），供 parser / stitcher / fact_engine 共用。

设计约束（见方案 v2.1 §7.1）：
- 物理页码是贯穿四层的唯一锚点，任何块都必须带 ``DocMeta.page``；
- 表格必须保持行列结构，禁止在缝合阶段做任何文本摘要或改写；
- 缝合产物 ``StitchedTable`` 必须能无损还原为 Markdown（可追溯）。

坐标约定
--------
``DocMeta.page`` 一律使用 **物理页码（1-based）**，与 PDF 阅读器
``#page=N`` 以及方案 §9.3 的 ``[Ref: {doc_id}#P{N}]`` 锚点保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

BlockKind = Literal["text", "heading", "table", "image", "code"]


@dataclass(frozen=True)
class DocMeta:
    """一个内容块的溯源坐标。

    Attributes:
        doc_id: 文档主键，格式 ``{股票代码}_{年度}_{报告类型}``，例如 ``002594_2025_年报``。
        page: 物理页码（1-based）。
        section: 所在章节标题，解析时由最近的标题块继承，可为 ``None``。
    """

    doc_id: str
    page: int
    section: str | None = None

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError(f"page 必须 >= 1（1-based 物理页码），收到 {self.page}")


@dataclass
class TableBlock:
    """单个分页表格（尚未跨页缝合）。

    Attributes:
        meta: 溯源坐标。
        header: 表头单元格；``None`` 表示该表格无表头（首行即数据行）。
        rows: 数据行（每个元素是一行的单元格列表）。
        has_separator: 原始 Markdown 是否包含 ``|---|---|`` 分隔行。
            保留该标记是为了区分「MinerU 输出自带表头」与「我们推断的表头」。
        block_index: 在所在页内的块序号（0-based），缝合时需要判断是否为页末/页首。
        raw_lines: 原始 Markdown 行，便于调试与回溯。
    """

    meta: DocMeta
    header: list[str] | None
    rows: list[list[str]]
    has_separator: bool = False
    block_index: int = 0
    raw_lines: list[str] = field(default_factory=list)

    @property
    def ncols(self) -> int:
        """列数。以表头为准；无表头时取首行长度。"""
        if self.header is not None:
            return len(self.header)
        return len(self.rows[0]) if self.rows else 0

    @property
    def page(self) -> int:
        return self.meta.page

    @property
    def is_headerless(self) -> bool:
        """是否无表头——跨页续接的核心判据（方案 §7.1）。"""
        return self.header is None


@dataclass
class Block:
    """一个通用内容块（文本 / 标题 / 表格 / 图片 / 代码）。

    Attributes:
        is_noise: 该块已被判定为页眉/页脚等重复噪音。噪音块**不删除**，
            而是就地打标：缝合阶段据此跳过它，审计阶段据此追溯它。
            需要物理移除时调用 ``noise_filter.drop_noise_blocks()``。
    """

    meta: DocMeta
    kind: BlockKind
    lines: list[str]
    block_index: int = 0
    table: TableBlock | None = None
    is_noise: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


@dataclass
class Page:
    """一页的内容块集合，块顺序即页内阅读顺序。"""

    number: int
    blocks: list[Block] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.blocks)


@dataclass
class StitchedTable:
    """跨页缝合后的逻辑表（对应 L1 抽取的最小单元）。

    Attributes:
        doc_id: 文档主键。
        header: 表头单元格；跨页续接时沿用起始页的表头。
        rows: 合并后的全部数据行。
        page_start / page_end: 覆盖的物理页码区间（单页时两者相等）。
        source_tables: 由多少张分页表格拼成；``>1`` 表示发生了缝合。
        parent_id: 逻辑表主键，跨页时形如 ``{doc_id}#P42-P43_T01``。
        table_id: 起始页的物理表主键，形如 ``{doc_id}#P42_T01``。
        section: 所在章节。
    """

    doc_id: str
    header: list[str] | None
    rows: list[list[str]]
    page_start: int
    page_end: int
    source_tables: int
    parent_id: str
    table_id: str
    section: str | None = None

    @property
    def ncols(self) -> int:
        if self.header is not None:
            return len(self.header)
        return len(self.rows[0]) if self.rows else 0

    @property
    def is_stitched(self) -> bool:
        return self.source_tables > 1

    def to_markdown(self) -> str:
        """还原为 Markdown 管道表，供 L1 抽取阶段直接消费。"""
        lines: list[str] = []
        if self.header is not None:
            lines.append(_render_row(self.header))
            lines.append(_render_row(["---"] * len(self.header)))
        for row in self.rows:
            lines.append(_render_row(row))
        return "\n".join(lines)


def _render_row(cells: list[str]) -> str:
    escaped = [cell.replace("|", "\\|") for cell in cells]
    return "| " + " | ".join(escaped) + " |"


@dataclass
class StitchRecord:
    """一次缝合操作的审计记录（供测试断言与质量报表使用）。"""

    doc_id: str
    pages: list[int]
    ncols: int
    reason: str


@dataclass
class StitchResult:
    """缝合阶段的完整产物。"""

    tables: list[StitchedTable] = field(default_factory=list)
    records: list[StitchRecord] = field(default_factory=list)

    @property
    def stitched_count(self) -> int:
        """发生跨页缝合的表数量。"""
        return sum(1 for table in self.tables if table.is_stitched)
