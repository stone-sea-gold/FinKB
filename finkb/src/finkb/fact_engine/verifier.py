"""把 LLM 给的 Observation 与原始表格逐字核对，再换算成规范单位。

方案里「数字只由 SQL 计算」的前半句是「数字必须能在原文里逐字找到」，
这一层就是那道闸：模型说什么不算数，``row/col`` 指到的单元格里必须真有那个数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from finkb.fact_engine.metric_dict import Metric
from finkb.fact_engine.selector import SourceTable, norm_label

_NUM = re.compile(r"^[+-]?[\d,，]*\.?\d+$")


def parse_number(text: object) -> float | None:
    """把 ``1,234.5`` / ``-`` / ``—`` 解析成 float；解析不了返回 None。"""
    s = str(text or "").strip().replace(" ", "").replace("\u3000", "")
    s = s.replace("，", ",").replace("−", "-").replace("（", "(").replace("）", ")")
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1].strip()
    if s.endswith("%") or s.endswith("％"):
        s = s[:-1].strip()
    if not s or s in {"-", "—", "－", "/"}:
        return None
    if not _NUM.match(s):
        return None
    try:
        value = float(s.replace(",", ""))
    except ValueError:
        return None
    return -value if negative else value


def unit_factor(unit_of_account: str) -> float:
    """账面单位 → 元 的倍数。"""
    return {"元": 1.0, "千元": 1e3, "万元": 1e4, "亿元": 1e8}[unit_of_account]


def norm_value(value: float, metric: Metric, unit_of_account: str) -> float:
    """按指标默认单位归一：金额→亿元，百分比/每股/数量原样。"""
    if metric.unit_default == "亿元":
        return round(value * unit_factor(unit_of_account) / 1e8, 6)
    return round(value, 6)


@dataclass(frozen=True)
class Verified:
    metric: Metric
    period: str
    value_raw: float
    raw_text: str
    value_norm: float
    match_level: str
    row: int
    col: int


def verify(
    obs: dict,
    table: SourceTable,
    metrics: dict[str, Metric],
    periods: dict[int, str],
    unit_of_account: str,
) -> tuple[Verified | None, str | None]:
    """核对一条 Observation。返回 ``(结果, 失败原因)``，二者恰有一个非空。"""
    try:
        metric_id = str(obs["metric_id"])
        row = int(obs["row"])
        col = int(obs["col"])
    except (KeyError, TypeError, ValueError):
        return None, "schema_invalid"

    metric = metrics.get(metric_id)
    if metric is None:
        return None, "metric_not_in_dict"
    if not (0 <= row < len(table.rows)) or not (1 <= col < len(table.rows[row])):
        return None, "schema_invalid"

    cell = table.rows[row][col]
    cell_num = parse_number(cell)
    claimed = parse_number(obs.get("value_raw", cell))
    if cell_num is None or claimed is None:
        return None, "literal_not_found"
    if abs(cell_num - claimed) > max(1e-6, abs(cell_num) * 1e-9):
        return None, "literal_not_found"

    period = periods.get(col)
    if period is None:
        return None, "schema_invalid"

    raw = str(obs.get("value_raw", "")).strip()
    cell_text = cell.strip()
    if raw and raw == cell_text:
        level = "exact"
    elif raw and raw.replace(",", "").replace(" ", "") == cell_text.replace(",", "").replace(" ", ""):
        level = "normalized"
    else:
        level = "numeric"

    return (
        Verified(
            metric=metric,
            period=period,
            value_raw=cell_num,
            raw_text=cell_text,
            value_norm=norm_value(cell_num, metric, unit_of_account),
            match_level=level,
            row=row,
            col=col,
        ),
        None,
    )


def is_key_row(label: str, metric: Metric) -> bool:
    """行标签是否真的像这个指标（防止模型把「营业收入增长额」对到营业收入）。"""
    text = norm_label(label)
    names = [metric.metric_name, *metric.aliases]
    return any(norm_label(n) and norm_label(n) in text for n in names)
