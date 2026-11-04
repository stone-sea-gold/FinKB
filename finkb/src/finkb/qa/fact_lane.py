"""事实通道：从问句抽槽位，用 SQL 在 facts_dedup 上取数。

数字只由 SQL 计算（ADR-001）；本模块不生成 SQL，只用模板查询，
输出带 fact_id / doc / page 的证据账本，供 verifier 做数字绑定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml

from finkb.fact_engine.metric_dict import Metric, load_metric_dict
from finkb.fact_engine.selector import FINKB_ROOT

FACT_DB = FINKB_ROOT / "data" / "facts.duckdb"
SCOPE_PATH = FINKB_ROOT / "config" / "wiki_scope.yaml"

PERIOD_RE = re.compile(r"(20\d{2})\s*年(?:\s*(上半年|一季度|前三季度|三季度|半年报|一季报|三季报))?")
CODE_RE = re.compile(r"\b(0\d{5}|6\d{5})\b")
REPORT_SUFFIX = {
    "2025": "年报", "H1": "半年报", "Q1": "一季报", "Q3": "三季报", "Q1-Q3": "三季报",
}
SUFFIX_BY_PERIOD = {"": "年报", "H1": "半年报", "Q1": "一季报", "Q3": "三季报", "Q1-Q3": "三季报"}


@dataclass
class Slot:
    companies: list[str] = field(default_factory=list)   # 公司代码
    company_names: list[str] = field(default_factory=list)
    periods: list[str] = field(default_factory=list)     # 归一化期间，按问句出现顺序
    metric: Metric | None = None
    operation: str = "single"


def _load_scope(path: str | Path | None = None) -> dict[str, str]:
    raw = yaml.safe_load(Path(path or SCOPE_PATH).read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    for item in raw.get("docs", []):
        name = str(item["company_name"])
        names[name] = str(item["company"])
        names[str(item["company"])] = str(item["company"])
        short = name.rstrip("AB")
        if short != name and len(short) >= 2:
            names.setdefault(short, str(item["company"]))
    return names


def _normalize_period(year: str, suffix: str | None) -> str:
    suffix = (suffix or "").strip()
    if suffix in ("上半年", "半年报"):
        return f"{year}H1"
    if suffix in ("一季度", "一季报"):
        return f"{year}Q1"
    if suffix in ("前三季度", "三季报"):
        return f"{year}Q1-Q3"
    if suffix == "三季度":
        return f"{year}Q3"
    return year


def extract(question: str) -> Slot:
    """规则槽位抽取；覆盖 gold 15 题的四种运算。"""
    names = _load_scope()
    slot = Slot()
    text = question.replace(" ", "")
    # 公司：按名称在问句中出现的位置排序（避免“美的集团、贵州茅台、长江电力”顺序丢失）
    hits: list[tuple[int, str, str]] = []
    for name, code in names.items():
        index = text.find(name)
        if index >= 0:
            hits.append((index, name, code))
    hits.sort()
    seen: set[str] = set()
    for _, name, code in hits:
        if code in seen:
            continue
        seen.add(code)
        slot.companies.append(code)
        slot.company_names.append(name)
    # 期间：按出现顺序
    for match in PERIOD_RE.finditer(question):
        period = _normalize_period(match.group(1), match.group(2))
        if period not in slot.periods:
            slot.periods.append(period)
    # 指标：全称 + 别名，取最长命中
    best: tuple[int, Metric] | None = None
    for metric in load_metric_dict():
        for candidate in (metric.metric_name, *metric.aliases):
            compact = candidate.replace(" ", "")
            if compact and compact in text and (best is None or len(compact) > best[0]):
                best = (len(compact), metric)
    slot.metric = best[1] if best else None
    # 运算
    if "同比增速" in question or "同比" in question:
        slot.operation = "yoy"
    elif "最高值比最低值" in question:
        slot.operation = "range"
    elif "合计" in question or "总计" in question:
        slot.operation = "sum"
    elif "增加" in question or "增长" in question:
        slot.operation = "diff_add"
    elif "减少" in question or "下降" in question:
        slot.operation = "diff_sub"
    return slot


def _suffix(period: str) -> str:
    for marker, suffix in SUFFIX_BY_PERIOD.items():
        if marker and period.endswith(marker):
            return suffix
    return "年报"


def _fetch(con: duckdb.DuckDBPyConnection, company: str, period: str, metric_id: str) -> dict | None:
    suffix = _suffix(period)
    row = con.execute(
        "SELECT fact_id, company, period, metric_id, value_norm, norm_unit, source_doc, page_num "
        "FROM facts_dedup WHERE company=? AND period=? AND metric_id=? AND source_doc LIKE ? "
        "ORDER BY fact_id LIMIT 1",
        [company, period, metric_id, f"%{suffix}"],
    ).fetchone()
    if row is None:
        row = con.execute(
            "SELECT fact_id, company, period, metric_id, value_norm, norm_unit, source_doc, page_num "
            "FROM facts_dedup WHERE company=? AND period=? AND metric_id=? ORDER BY fact_id LIMIT 1",
            [company, period, metric_id],
        ).fetchone()
    if row is None:
        return None
    return {
        "fact_id": int(row[0]), "company": row[1], "period": row[2], "metric_id": row[3],
        "value": float(row[4]), "unit": row[5], "doc": row[6], "page": int(row[7]),
    }


def _ledger_entry(index: int, item: dict) -> dict:
    return {
        "id": f"c{index}",
        "value": item["value"],
        "unit": item["unit"],
        "fact_id": item["fact_id"],
        "doc": item["doc"],
        "page": item["page"],
        "metric_id": item["metric_id"],
        "company": item["company"],
        "period": item["period"],
    }


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def answer(question: str) -> dict | None:
    """回答事实类问题；抽不出槽位或查不到数据时返回 None（交给其它通道）。"""
    slot = extract(question)
    if slot.metric is None or not slot.companies:
        return None
    metric = slot.metric
    if slot.operation == "yoy" and len(slot.periods) == 1:
        # 同比只给了一个期间：自动补上年同期（如 2026H1 → 2025H1）。
        period = slot.periods[0]
        year = int(period[:4])
        slot.periods.append(f"{year - 1}{period[4:]}")
    con = duckdb.connect(str(FACT_DB), read_only=True)
    try:
        rows: list[dict] = []
        for company in slot.companies:
            for period in slot.periods:
                item = _fetch(con, company, period, metric.metric_id)
                if item is None:
                    return None
                rows.append(item)
    finally:
        con.close()

    ledger = [_ledger_entry(i + 1, item) for i, item in enumerate(rows)]
    unit = rows[0]["unit"]
    name = metric.metric_name
    companies = "、".join(slot.company_names)

    def derived(index: int, value: float, formula: str, unit_text: str) -> dict:
        entry = {"id": f"c{index}", "value": value, "unit": unit_text, "kind": "derived", "formula": formula}
        ledger.append(entry)
        return entry

    if slot.operation == "single":
        item = rows[0]
        value = item["value"]
        text = f"{slot.company_names[0]} {slot.periods[0]} 年的{name}为 {{{{c1}}}} {unit}。"
    elif slot.operation in ("diff_add", "diff_sub"):
        if len(rows) < 2:
            return None
        current, prior = rows[0], rows[1]
        value = current["value"] - prior["value"] if slot.operation == "diff_add" else prior["value"] - current["value"]
        entry = derived(len(ledger) + 1, value, f"{'c1-c2' if slot.operation == 'diff_add' else 'c2-c1'}", unit)
        word = "增加" if slot.operation == "diff_add" else "减少"
        text = (
            f"{slot.company_names[0]} {current['period']} 年的{name}为 {{{{c1}}}} {unit}，"
            f"{prior['period']} 年为 {{{{c2}}}} {unit}，{word} {{{{c3}}}} {unit}。"
        )
        assert entry["id"] == "c3"
    elif slot.operation == "yoy":
        if len(rows) < 2:
            return None
        current, prior = rows[0], rows[1]
        value = (current["value"] - prior["value"]) / prior["value"] * 100
        derived(len(ledger) + 1, value, "(c1-c2)/c2*100", "%")
        text = (
            f"{slot.company_names[0]} {current['period']} 年的{name}为 {{{{c1}}}} {unit}，"
            f"{prior['period']} 年为 {{{{c2}}}} {unit}，同比增速 {{{{c3}}}}%。"
        )
        unit = "%"
    elif slot.operation == "sum":
        value = sum(item["value"] for item in rows)
        derived(len(ledger) + 1, value, "sum(c*)", unit)
        refs = "、".join(f"{{{{c{i + 1}}}}}" for i in range(len(rows)))
        text = f"{companies} {slot.periods[0]} 年的{name}合计为 {{{{c{len(rows) + 1}}}}} {unit}（{refs} 相加）。"
    elif slot.operation == "range":
        values = [item["value"] for item in rows]
        value = max(values) - min(values)
        derived(len(ledger) + 1, value, "max(c*)-min(c*)", unit)
        refs = "、".join(f"{{{{c{i + 1}}}}}" for i in range(len(rows)))
        text = f"{companies} {slot.periods[0]} 年的{name}最高值比最低值多 {{{{c{len(rows) + 1}}}}} {unit}（{refs}）。"
    else:
        return None
    return {
        "route": "fact",
        "question": question,
        "refused": False,
        "slots": {
            "companies": slot.companies, "company_names": slot.company_names,
            "periods": slot.periods, "metric_id": metric.metric_id, "operation": slot.operation,
        },
        "value": value,
        "unit": unit,
        "text": text,
        "ledger": ledger,
        "citations": [f"[Ref: {item['doc']}#P{item['page']}]" for item in rows],
    }
