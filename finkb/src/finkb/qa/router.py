"""三路路由：规则优先（事实 / 本体+关系 / 原文），不确定时交给原文通道。"""

from __future__ import annotations

from dataclasses import dataclass, field

from finkb.qa.fact_lane import extract

ONTOLOGY_KEYWORDS = (
    "资产负债率超过", "高杠杆", "现金流背离", "经营现金流为负", "经营现金流为负",
    "研发强度", "研发投入占", "净资产收益率低", "ROE 低", "现金充裕", "应收账款高", "存货高",
    "高风险", "风险标签", "属于哪一类", "哪些公司",
    "子公司", "股东", "担保", "关联方", "间接担保", "股权", "控制链", "附注",
)
TEXT_KEYWORDS = ("为什么", "如何", "怎么", "哪些", "描述", "情况", "进展", "政策", "规划", "原因", "措施", "说明", "风险")
TEXT_INTENT_KEYWORDS = ("如何", "规模", "人员", "情况", "进展", "措施", "原因", "说明", "描述", "布局", "结构", "构成", "政策", "规划", "风险")
NUMERIC_STRICT_RE = __import__("re").compile(r"(多少|几|增速|合计|最高值|最低值|相比|增加|减少)")


@dataclass
class Route:
    name: str
    confidence: float
    signals: list[str] = field(default_factory=list)


def route(question: str) -> Route:
    slot = extract(question)
    signals: list[str] = []
    if "附注" in question and slot.metric is not None and slot.companies:
        signals.append("note-drill")
        return Route("ontology", 0.85, signals)
    if any(keyword in question for keyword in TEXT_INTENT_KEYWORDS) and not NUMERIC_STRICT_RE.search(question):
        signals.append("text-intent")
        return Route("text", 0.75, signals)
    if slot.metric is not None and slot.companies and slot.periods:
        signals.extend([f"metric:{slot.metric.metric_id}", f"companies:{','.join(slot.companies)}", f"periods:{','.join(slot.periods)}"])
        return Route("fact", 0.9, signals)
    if any(keyword in question for keyword in ONTOLOGY_KEYWORDS):
        signals.append("ontology-keyword")
        return Route("ontology", 0.8, signals)
    if slot.metric is not None and slot.companies:
        signals.append("metric-no-period")
        return Route("fact", 0.5, signals)
    if any(keyword in question for keyword in TEXT_KEYWORDS):
        signals.append("text-keyword")
    return Route("text", 0.6 if signals else 0.4, signals)
