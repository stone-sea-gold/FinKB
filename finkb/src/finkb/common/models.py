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
        section_rows: 「全宽合并行」清单 ``[(数据行下标, 文本), ...]``。
            这些行在展开 ``colspan`` 后退化为「首列有值、其余全空」，
            仅看数据无法与普通数据行区分，必须在解析阶段显式记录。
        header_rows: 表头各层原貌（已展开合并单元格）。空表示无表头。
            ``header`` 是它的压平视图，供只需要一行列名的下游使用。
        table_title: 表标题。MinerU 会把「公司及其子公司对外担保情况」这类
            标题塞进表格第一行并跨满全宽，它不是表头，抽出来单独保存。
    """

    meta: DocMeta
    header: list[str] | None
    rows: list[list[str]]
    has_separator: bool = False
    block_index: int = 0
    raw_lines: list[str] = field(default_factory=list)
    section_rows: list[tuple[int, str]] = field(default_factory=list)
    header_rows: list[list[str]] = field(default_factory=list)
    table_title: str | None = None

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
    #: 跨页拼接后累计的「全宽合并行」，下标相对 ``rows``。
    section_rows: list[tuple[int, str]] = field(default_factory=list)

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


SectionKind = Literal["data", "summary", "kv", "kv_row", "footnote"]


@dataclass
class LogicalTable:
    """**逻辑数据表** —— 物理表按内部小标题切分后的语义单元。

    与 :class:`StitchedTable` 的区别：后者是「版面事实」（MinerU 眼里的一个
    ``<table>``，可能横跨多页），前者是「语义事实」（一个标题 + 一套表头 + 一段数据）。

    实测依据（比亚迪 2025 年报 P89–P91）：一个 11 列 85 行的物理表内部实际是
    **3 张数据表 + 1 个汇总块**，靠 ``colspan`` 全宽行分隔。

    Attributes:
        logical_id: 逻辑表主键，形如 ``{parent_id}#S01``。
        parent_id: 指向物理表，保留版面溯源。
        header: 列名。**切分不产生无表头的子表**——无自有表头时继承父表表头，
            这是本模型存在的核心原因（见 ``header_source``）。
        header_source: ``self`` 表示该段自带表头（表头行已从 rows 中剥离）；
            ``inherited`` 表示继承自父表，rows 里不含表头行。
        section_path: 分节路径。记录切分点上的全宽行文本（如
            ``["三、筹资活动产生的现金流量:"]``），供 L1 拼接完整科目路径。
        rows: 该段的数据行（含切分点那一行本身，避免信息丢失）。
        page_start / page_end: 覆盖的物理页码。
        row_range: 在父表 ``rows`` 中的区间 ``[start, end)``。
        kind: ``data`` 常规表格 / ``summary`` 汇总块 / ``kv`` 键值对段落 /
            ``kv_row`` 单行横表 / ``footnote`` 注释段。
        pairs: **仅 ``kv_row`` 使用**。「单行横表」解析出的 ``(键, 值)`` 列表。
            这类行不是「一行数据」，而是同一行里并排的多组键值对，例如
            ``报告期末普通股股东总数 | 642,600 | 报告期末表决权恢复的优先股股东总数 | 0``。
        orphan_values: **仅 ``kv_row`` 使用**。无法配对的单元格。
            实测 P109：首格标签在 MinerU 输出里丢失（HTML 里是空 ``<td>``），
            导致该行呈 ``值|键|值|键|值|键|值`` 的奇数形态；剥离首个孤儿值后，
            其余 6 格恰好配成 3 组正确语义的键值对。
            孤儿值**不放进 rows**（避免被当作数据行），但保留在此以防信息丢失。
    """

    logical_id: str
    parent_id: str
    header: list[str] | None
    header_source: Literal["self", "inherited"]
    section_path: list[str]
    rows: list[list[str]]
    page_start: int
    page_end: int
    row_range: tuple[int, int]
    kind: SectionKind = "data"
    pairs: list[tuple[str, str]] = field(default_factory=list)
    orphan_values: list[str] = field(default_factory=list)
    header_rows: list[list[str]] = field(default_factory=list)
    table_title: str | None = None

    @property
    def ncols(self) -> int:
        if self.header is not None:
            return len(self.header)
        return len(self.rows[0]) if self.rows else 0

    @property
    def title(self) -> str | None:
        """该段的小标题（分节路径最后一段）。"""
        return self.section_path[-1] if self.section_path else None

    def to_markdown(self) -> str:
        """渲染为 Markdown；有标题时先输出标题行。"""
        lines: list[str] = []
        for label in self.section_path:
            lines.append(f"**{label}**")

        if self.kind == "kv_row" and self.pairs:
            lines.append(_render_row(["键", "值"]))
            lines.append(_render_row(["---", "---"]))
            for key, value in self.pairs:
                lines.append(_render_row([key, value]))
            for orphan in self.orphan_values:
                lines.append(f"> 未配对值（原标签丢失）：`{orphan}`")
            return "\n".join(lines)

        if self.header is not None:
            lines.append(_render_row(self.header))
            lines.append(_render_row(["---"] * len(self.header)))
        for row in self.rows:
            lines.append(_render_row(row))
        return "\n".join(lines)


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
    #: 疑似续接但被判据拒绝的记录。**必须可见**——静默拒绝会让
    #: 「为什么没缝上」变成无法排查的谜题（实测 P43→P44 就是这种情况）。
    rejections: list[StitchRecord] = field(default_factory=list)

    @property
    def stitched_count(self) -> int:
        """发生跨页缝合的表数量。"""
        return sum(1 for table in self.tables if table.is_stitched)
