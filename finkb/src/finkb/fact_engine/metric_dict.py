"""L1 指标字典的加载与校验。

数据源是 ``config/metric_dict.yaml``（唯一真相），本模块只做两件事：
把 YAML 读成 :class:`Metric`，以及把 ``unit_default`` 推导成 P9 需要的
``unit_family`` / ``norm_unit``。不碰数据库——入库在 :mod:`finkb.fact_engine.store`。

为什么 ``unit_default`` 能推导出单位族：字典里只收「财报直接披露」的指标，
单位写法是封闭的四种（亿元 / % / 元/股 / 辆）。新增单位必须同时补
:data:`UNIT_DERIVATION`，否则加载直接报错——宁可启动失败，也不要静默把
「元/股」当成「元」去做 ÷1e8。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

FINKB_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_METRIC_DICT = FINKB_ROOT / "config" / "metric_dict.yaml"

# unit_default -> (unit_family, norm_unit)，对齐方案 v2.2 P9
UNIT_DERIVATION: dict[str, tuple[str, str]] = {
    "亿元": ("money", "亿元"),
    "元": ("money", "元"),
    "%": ("percent", "%"),
    "元/股": ("ratio", "元/股"),
    "辆": ("count", "个"),
}

CATEGORIES = {"盈利", "结构", "现金流", "资产", "研发", "每股", "销量"}


@dataclass(frozen=True)
class Metric:
    """指标字典的一行。

    Attributes:
        metric_id: 稳定英文 ID，事实表外键。
        metric_name: 中文显示名。
        unit_default: 财报披露单位；同时决定 ``unit_family`` 与 ``norm_unit``。
        category: 分类，供 Text-to-SQL 与看板分组。
        aliases: 非标写法，供 LLM 对齐与别名扩展。
    """

    metric_id: str
    metric_name: str
    unit_default: str
    category: str
    aliases: tuple[str, ...]

    @property
    def unit_family(self) -> str:
        """P9 单位族：money / percent / ratio / count。"""
        return UNIT_DERIVATION[self.unit_default][0]

    @property
    def norm_unit(self) -> str:
        """P9 归一化目标单位。"""
        return UNIT_DERIVATION[self.unit_default][1]


def load_metric_dict(path: str | Path | None = None) -> list[Metric]:
    """读取并校验指标字典。

    Raises:
        ValueError: 文件结构不对、ID 重复、单位/分类不在白名单。
    """
    p = Path(path) if path is not None else DEFAULT_METRIC_DICT
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("metrics"), list):
        raise ValueError(f"{p}: 顶层必须是 {{'metrics': [...]}}")

    seen: set[str] = set()
    out: list[Metric] = []
    for i, item in enumerate(raw["metrics"]):
        if not isinstance(item, dict):
            raise ValueError(f"{p}: metrics[{i}] 不是映射")
        metric_id = item.get("metric_id")
        if not metric_id:
            raise ValueError(f"{p}: metrics[{i}] 缺 metric_id")
        if metric_id in seen:
            raise ValueError(f"{p}: metric_id 重复: {metric_id}")
        seen.add(metric_id)

        unit = item.get("unit_default")
        if unit not in UNIT_DERIVATION:
            raise ValueError(
                f"{p}: {metric_id} 的 unit_default={unit!r} 不在白名单 "
                f"{sorted(UNIT_DERIVATION)}"
            )
        category = item.get("category")
        if category not in CATEGORIES:
            raise ValueError(f"{p}: {metric_id} 的 category={category!r} 不在白名单")

        aliases = item.get("aliases") or []
        if not isinstance(aliases, list) or not all(isinstance(a, str) and a for a in aliases):
            raise ValueError(f"{p}: {metric_id} 的 aliases 必须是非空字符串列表")

        out.append(
            Metric(
                metric_id=metric_id,
                metric_name=item["metric_name"],
                unit_default=unit,
                category=category,
                aliases=tuple(aliases),
            )
        )
    if not out:
        raise ValueError(f"{p}: 指标字典为空")
    return out
