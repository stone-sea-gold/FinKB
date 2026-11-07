"""P7 跨期对比：同一公司、同一指标、多个期间并排取数。

数字仍只由事实库 SQL 取出（ADR-001）；本模块只做“选期间 + 排版”，
不调用 LLM。MCP ``compare`` 工具与 ``finkb compare`` 子命令共用这里的实现。
"""

from __future__ import annotations

import re

import duckdb

from finkb.fact_engine.selector import FINKB_ROOT
from finkb.qa import fact_lane, pipeline
from finkb.qa import understanding as understanding_mod
from finkb.qa.synthesizer import render

FACT_DB = FINKB_ROOT / "data" / "facts.duckdb"
MIN_PERIODS = 2

_PERIOD_SUFFIX = {"上半年": "H1", "一季度": "Q1", "前三季度": "Q1-Q3", "三季度": "Q3"}
_CANONICAL = re.compile(r"^(20\d{2})(H1|Q1-Q3|Q1|Q3)?$")
_YEAR_FORM = re.compile(r"^(20\d{2})(?:年)?(度|上半年|一季度|前三季度|三季度)?$")
_RANGE = re.compile(r"(20\d{2})\s*[-–—~至到]\s*(20\d{2})")
_PERIOD_TOKEN = re.compile(
    r"20\d{2}(?:H1|Q1-Q3|Q1|Q3)|20\d{2}\s*年?(?:度|上半年|一季度|前三季度|三季度)?"
)


def canonical_period(text: str) -> str | None:
    """把用户写法归一到事实库期间：2024 / 2024年 / 2024H1 / 2024上半年。"""
    value = str(text or "").strip().replace(" ", "")
    match = _CANONICAL.match(value)
    if match:
        return match.group(1) + (match.group(2) or "")
    match = _YEAR_FORM.match(value)
    if not match:
        return None
    year, kind = match.group(1), match.group(2)
    if kind in (None, "度"):
        return year
    return year + _PERIOD_SUFFIX[kind]


def question_periods(question: str) -> list[str]:
    """从问题里收集全部期间；支持“2023-2025”这种区间展开。"""
    result: list[str] = []
    rest = _RANGE.sub(" ", question)
    for start, end in _RANGE.findall(question):
        first, last = int(start), int(end)
        if not 0 < last - first <= 10:
            continue
        for year in range(first, last + 1):
            period = str(year)
            if period not in result:
                result.append(period)
    for token in _PERIOD_TOKEN.findall(rest):
        period = canonical_period(token)
        if period and period not in result:
            result.append(period)
    return result


def _targets(question: str, periods: list[str] | None) -> list[str]:
    candidates = list(periods) if periods else question_periods(question)
    result: list[str] = []
    for raw in candidates:
        period = canonical_period(raw)
        if period and period not in result:
            result.append(period)
    return result


def compare(question: str, *, periods: list[str] | None = None) -> dict:
    """同一公司、同一指标、多个期间并排返回数值、单位与引用。"""
    trace: list[dict] = [{"step": "compare", "question": question}]
    u = understanding_mod.understand(question)
    if u.metric is None or not u.companies:
        return pipeline._refusal(
            question,
            "metric",
            "对比需要明确一个指标和公司，例如：美的集团 2023–2025 年营业收入。",
            trace,
            reason="compare-slots",
        )
    targets = _targets(question, periods)
    if len(targets) < MIN_PERIODS:
        return pipeline._refusal(
            question,
            "metric",
            "对比至少需要两个期间，例如：2023、2024、2025。",
            trace,
            reason="compare-periods",
        )

    company = u.companies[0]
    company_name = u.company_names[0] if u.company_names else company
    metric = u.metric
    con = duckdb.connect(str(FACT_DB), read_only=True)
    try:
        items = [(period, fact_lane._fetch(con, company, period, metric.metric_id)) for period in targets]
    finally:
        con.close()

    ledger: list[dict] = []
    parts: list[str] = []
    rows: list[dict] = []
    for period, item in items:
        label = fact_lane._period_label(period)
        if item is None:
            rows.append({
                "period": period, "label": label, "value": None, "unit": None,
                "raw_value": None, "raw_unit": None, "doc": None, "page": None, "role": None,
            })
            parts.append(f"{label}无数据")
            continue
        entry = fact_lane._ledger_entry(len(ledger) + 1, item)
        ledger.append(entry)
        rows.append({
            "period": item["period"], "label": label, "value": item["value"], "unit": item["unit"],
            "raw_value": item["raw_value"], "raw_unit": item["raw_unit"],
            "doc": item["doc"], "page": item["page"], "role": item["role"],
        })
        parts.append(f"{label}为 {{{{c{entry['id'][1:]}}}}} {item['unit']}{fact_lane._raw_note(item)}")

    found = [row for row in rows if row["value"] is not None]
    if not found:
        return pipeline._refusal(
            question,
            "metric",
            f"没有找到 {company_name} 这些期间的{metric.metric_name}。",
            trace,
            reason="compare-no-facts",
        )

    text, _ = render(f"{company_name} {metric.metric_name}：" + "；".join(parts) + "。", ledger)
    payload = {
        "route": "fact",
        "refused": False,
        "slots": {
            "companies": [company],
            "company_names": [company_name],
            "periods": targets,
            "metric_id": metric.metric_id,
            "operation": "compare",
        },
        "text": text,
        "ledger": ledger,
        "citations": [f"[Ref: {row['doc']}#P{row['page']}]" for row in found],
        "data": {
            "metric_id": metric.metric_id,
            "metric_name": metric.metric_name,
            "company": company,
            "company_name": company_name,
            "unit": found[0]["unit"],
            "periods": targets,
            "rows": rows,
        },
    }
    trace.append({"step": "compare", "periods": targets, "found": len(found)})
    return pipeline._envelope(question, "metric", payload, trace)
