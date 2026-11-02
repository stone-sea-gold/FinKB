"""分产品 / 分行业 / 分地区结构表的结构化抽取（ADR-045）。

为什么不用 LLM：这类表的列名是固定的「营业收入 / 营业成本 / 毛利率」，
行是「分产品」「分行业」标记 + 若干段名，数字直接可读。让模型再抄一遍只会
多一层出错面，而 L2 的探针 02 恰恰要求结构准确。所以这里**确定性解析**：
按列名定位、按行标记切段、逐格解析数字，解析不了就跳过（不猜）。

输出进 ``segment_facts`` 长表：一行一个（维度, 段名, 指标）。金额统一归一到
亿元，毛利率保留百分数原值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from finkb.fact_engine.selector import SourceTable, load_tables, norm_label
from finkb.fact_engine.store import FactStore
from finkb.fact_engine.verifier import parse_number, unit_factor

# 行标记 → 维度名。只收这三种；「分销售模式」等暂不进入 MVP。
DIMENSIONS = {"分产品": "product", "分行业": "industry", "分地区": "region"}

# 维度种类的封闭词表（ADR-054）：dim_kind → 中文标签。
# 维度**种类**由准则与披露规则决定，是个小集合；维度**成员**是数据驱动的开放
# 集合，从结构表行名自动抽取。所以新增一份年报不需要任何维度配置。
DIMENSION_LABELS = {"product": "分产品", "industry": "分行业", "region": "分地区"}
DIMENSION_MARKERS = {"product": "分产品", "industry": "分行业", "region": "分地区"}

# 列名变体：多数用「营业收入 / 营业成本 / 毛利率」，中国神华用「销售收入 / 销售成本」。
REVENUE_NEEDLES = ("营业收入", "销售收入", "主营业务收入")
COST_NEEDLES = ("营业成本", "销售成本", "主营业务成本")
MARGIN_NEEDLES = ("毛利率",)

# 结构指标名（segment_facts.metric）
SEGMENT_METRICS = ("segment_revenue", "segment_cost", "segment_gross_margin")


def _column(
    header: list[str], needles: tuple[str, ...], *, exclude: tuple[str, ...] = ()
) -> int | None:
    """在表头里找第一个包含任一 ``needle`` 且不含任何 ``exclude`` 词的列。"""
    for i, cell in enumerate(header):
        label = norm_label(cell)
        if any(needle in label for needle in needles) and not any(
            word in label for word in exclude
        ):
            return i
    return None


def _dimension_in_text(text: str) -> str | None:
    """从一段文字里认维度。子串匹配：'主营业务分产品情况' 也算 product。"""
    label = norm_label(text)
    for marker, dimension in DIMENSIONS.items():
        if marker in label:
            return dimension
    return None


def _dimension_of(section_path: tuple[str, ...]) -> str | None:
    for part in section_path:
        dimension = _dimension_in_text(part)
        if dimension:
            return dimension
    return None


def _dimension_of_table(table: SourceTable) -> str | None:
    """维度标记有三种落点：表头前两格、section_path、行内标记行。都认。"""
    for cell in table.header[:2]:
        dimension = _dimension_in_text(cell)
        if dimension:
            return dimension
    dimension = _dimension_of(table.section_path)
    if dimension:
        return dimension
    for row in table.rows:
        if row:
            dimension = DIMENSIONS.get(norm_label(row[0]))
            if dimension:
                return dimension
    return None


def _has_segment_columns(table: SourceTable) -> bool:
    header = table.header
    return (
        _column(header, REVENUE_NEEDLES, exclude=("增减", "比重", "成本")) is not None
        and _column(header, COST_NEEDLES, exclude=("增减", "比重")) is not None
        and _column(header, MARGIN_NEEDLES, exclude=("增减", "比重")) is not None
    )


def find_segment_tables(parents: list[SourceTable]) -> list[SourceTable]:
    """挑出「收入 + 成本 + 毛利率」三列齐全、且有维度标记的表。

    维度标记有两种落点：① 表头首格 / 行里的「分产品」等标记；② 分节切分把
    标记行吃成了逻辑表的 ``section_path``（实测比亚迪 T13 就是后者）。两处都认。
    """
    out: list[SourceTable] = []
    for table in parents:
        if not _has_segment_columns(table):
            continue
        if _dimension_of_table(table) or any(
            _dimension_of_table(child) for child in table.children
        ):
            out.append(table)
    return out


_UNITS = ("元", "千元", "万元", "百万元", "亿元")


def _expected_revenue(store: FactStore, source: SourceTable) -> float | None:
    """同一份文档里 facts 记录的总收入（亿元），用于反推结构表单位。"""
    row = store.conn.execute(
        "select max(value_norm) from facts "
        "where source_doc = ? and metric_id = 'revenue' and period = ?",
        [source.doc_id, source.period],
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _revenue_sum(table: SourceTable) -> float | None:
    rev_col = _column(table.header, REVENUE_NEEDLES, exclude=("增减", "比重", "成本"))
    if rev_col is None:
        return None
    total = 0.0
    found = False
    for row in table.rows:
        if rev_col >= len(row):
            continue
        value = parse_number(row[rev_col])
        if value is None:
            continue
        total += value
        found = True
    return total if found else None


def _infer_unit(store: FactStore, source: SourceTable) -> str | None:
    """页面没写单位时，用「本表收入合计 ≈ facts 总收入」反推记账单位。

    只在误差小于 5% 时采信；否则返回 None，交回 default_unit。
    """
    expected = _expected_revenue(store, source)
    raw = _revenue_sum(source)
    if not expected or raw is None or raw <= 0:
        return None
    best: str | None = None
    best_gap = 1.0
    for unit in _UNITS:
        gap = abs(raw * unit_factor(unit) / 1e8 - expected) / expected
        if gap < best_gap:
            best, best_gap = unit, gap
    return best if best_gap < 0.05 else None


def _to_yi(value: float, unit_of_account: str) -> float:
    return round(value * unit_factor(unit_of_account) / 1e8, 6)


def parse_segments(
    table: SourceTable, unit_of_account: str = "元", dimension: str | None = None
) -> list[dict]:
    """把一张结构表解析成 ``segment_facts`` 行；解析不出返回空列表。

    只取「当前期间」那一组列（表头里第一个不带「增减/比重」的收入列）。
    表格若按年份分成两组列（2025年/2024年），取第一组——年报正文的当年口径。
    ``dimension`` 显式给出时（来自表头标记或 section_path），不再依赖行内标记。
    """
    rev_col = _column(table.header, REVENUE_NEEDLES, exclude=("增减", "比重", "成本"))
    cost_col = _column(table.header, COST_NEEDLES, exclude=("增减", "比重"))
    margin_col = _column(table.header, MARGIN_NEEDLES, exclude=("增减", "比重"))
    if rev_col is None or cost_col is None or margin_col is None:
        return []

    rows: list[dict] = []
    current: str | None = dimension or _dimension_of_table(table)
    seen: set[tuple[str, str, str]] = set()
    for row in table.rows:
        if not row:
            continue
        first = norm_label(row[0])
        marker = DIMENSIONS.get(first)
        if marker:
            current = marker
            continue
        if current is None or not first:
            continue
        # 小计/合计/总计是汇总行，收了会和明细重复计数（券商的「境内小计」也是）。
        if first in {"合计", "小计", "总计"} or first.endswith(("合计", "小计", "总计")):
            continue
        # 「其中:楼宇科技」是上一段的内部拆分，收了会重复计数。
        if first.startswith("其中"):
            continue
        if rev_col >= len(row):
            continue
        revenue = parse_number(row[rev_col])
        if revenue is None:
            continue
        segment = str(row[0]).strip()
        values = [
            ("segment_revenue", _to_yi(revenue, unit_of_account), "亿元"),
        ]
        cost = parse_number(row[cost_col]) if cost_col < len(row) else None
        if cost is not None:
            values.append(("segment_cost", _to_yi(cost, unit_of_account), "亿元"))
        margin = parse_number(row[margin_col]) if margin_col < len(row) else None
        if margin is not None:
            values.append(("segment_gross_margin", round(margin, 6), "%"))
        for metric, value, unit in values:
            key = (current, segment, metric)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "dimension": current,
                    "segment_name": segment,
                    "metric": metric,
                    "value_norm": value,
                    "unit": unit,
                }
            )
    return rows


def _revenue_by_period(store: FactStore, doc_id: str) -> dict[str, float]:
    """同一份文档里 facts 记录的各期总收入（亿元）。"""
    rows = store.conn.execute(
        "select period, max(value_norm) from facts "
        "where source_doc = ? and metric_id = 'revenue' group by period",
        [doc_id],
    ).fetchall()
    return {str(period): float(value) for period, value in rows if value is not None}


def _close(a: float, b: float, tolerance: float = 0.01) -> bool:
    return bool(b) and abs(a - b) / abs(b) < tolerance


def plan_segments(
    store: FactStore,
    doc_dir: Path,
    manifest: dict,
    default_unit: str = "元",
    unit_for: Callable[[SourceTable], str | None] | None = None,
) -> list[tuple[SourceTable, str | None, str]]:
    """列出要解析的（表, 维度, 单位），并剔除「上一期对比表」。

    半年报/年报里常并排放两张同结构表：本期一张、上期一张（表头都不写年份）。
    用 facts 的同期/上期总收入判别：收入合计等于上期总收入的，是对比表，丢弃。
    """
    from finkb.fact_engine.ingest import unit_from_text

    plan: list[tuple[SourceTable, str | None, str]] = []
    for table in find_segment_tables(load_tables(doc_dir, manifest)):
        sources: list[tuple[SourceTable, str | None]] = [
            (child, _dimension_of_table(child))
            for child in table.children
            if _dimension_of_table(child)
        ]
        if not sources:
            sources = [(table, _dimension_of_table(table))]
        for source, dimension in sources:
            unit = (
                (unit_for(source) if unit_for else None)
                or unit_from_text("".join(source.header))
                or _infer_unit(store, source)
                or default_unit
            )
            plan.append((source, dimension, unit))

    return _drop_earlier_tables(plan, _revenue_by_period(store, doc_dir.name))


def _drop_earlier_tables(
    plan: list[tuple[SourceTable, str | None, str]], by_period: dict[str, float]
) -> list[tuple[SourceTable, str | None, str]]:
    """丢掉收入合计等于「上期总收入」的表——那是并排印的对比期表。"""
    if not by_period:
        return plan
    kept: list[tuple[SourceTable, str | None, str]] = []
    for source, dimension, unit in plan:
        raw = _revenue_sum(source)
        current = by_period.get(source.period)
        if raw is None or not current:
            kept.append((source, dimension, unit))
            continue
        total = raw * unit_factor(unit) / 1e8
        earlier = [value for period, value in by_period.items() if period != source.period]
        if not _close(total, current) and any(_close(total, value) for value in earlier):
            continue
        kept.append((source, dimension, unit))
    return kept


def write_segments(
    store: FactStore,
    doc_dir: Path,
    manifest: dict,
    default_unit: str,
    unit_for: Callable[[SourceTable], str | None] | None = None,
) -> int:
    """解析一份文档的结构表并写库，返回写入行数。

    写入前先把维度种类登记进 ``dimensions``（封闭词表、幂等）；每条结构事实
    同时把段名归一成 ``dimension_members`` 的一个成员，``member_id`` 作为外键落在
    ``segment_facts`` 上。这样「分产品」下的段名是数据驱动的：新公司、新年报、
    新段名都自动长出来，不需要改任何配置。
    """
    for dim_kind, label in DIMENSION_LABELS.items():
        store.upsert_dimension(
            dim_kind=dim_kind, label=label, markers=DIMENSION_MARKERS[dim_kind],
            note="维度种类固定，成员由结构表行名自动抽取",
        )

    written = 0
    for source, dimension, unit in plan_segments(
        store, doc_dir, manifest, default_unit, unit_for
    ):
        for item in parse_segments(source, unit, dimension=dimension):
            member_id = store.upsert_segment_member(
                dim_kind=item["dimension"],
                raw_label=item["segment_name"],
                source_doc=source.doc_id,
            )
            store.insert_segment_fact(
                company=source.company,
                period=source.period,
                dimension=item["dimension"],
                segment_name=item["segment_name"],
                metric=item["metric"],
                value_norm=item["value_norm"],
                unit=item["unit"],
                source_doc=source.doc_id,
                page_num=source.page,
                table_id=source.logical_id,
                member_id=member_id,
            )
            written += 1
    return written
