"""事实通道：从问句抽槽位，用 SQL 在 facts_dedup 上取数。

数字只由 SQL 计算（ADR-001）；本模块不生成 SQL，只用模板查询，
输出带 fact_id / doc / page 的证据账本，供 verifier 做数字绑定。

问句理解统一交给 ``qa/understanding.py``（规则 + 可选 LLM + 时间覆盖守卫）：
- 显式时间词没被解析 → 明确拒答，不再默认年报；
- 答案用人类可读期间，并保留原文数值/单位供核对。
"""

from __future__ import annotations

from finkb.fact_engine.selector import FINKB_ROOT
from finkb.qa.understanding import Understanding, _period_label, understand

FACT_DB = FINKB_ROOT / "data" / "facts.duckdb"

# 向后兼容：旧代码/测试把 extract() 的结果叫 Slot。
Slot = Understanding

SUFFIX_BY_PERIOD = {"": "年报", "H1": "半年报", "Q1": "一季报", "Q3": "三季报", "Q1-Q3": "三季报"}


def extract(question: str) -> Understanding:
    return understand(question)


def _suffix(period: str) -> str:
    for marker, suffix in SUFFIX_BY_PERIOD.items():
        if marker and period.endswith(marker):
            return suffix
    return "年报"


def _fetch(con, company: str, period: str, metric_id: str) -> dict | None:
    columns = (
        "fact_id, company, period, metric_id, value_norm, norm_unit, "
        "value_raw, unit_of_account, source_doc, page_num"
    )
    row = con.execute(
        f"SELECT {columns} FROM facts_dedup WHERE company=? AND period=? AND metric_id=? "
        "AND source_doc LIKE ? "
        "ORDER BY CASE WHEN source_doc LIKE ? THEN 0 ELSE 1 END, fact_id LIMIT 1",
        [company, period, metric_id, f"%{_suffix(period)}", f"{company}_{period[:4]}%"],
    ).fetchone()
    if row is None:
        row = con.execute(
            f"SELECT {columns} FROM facts_dedup WHERE company=? AND period=? AND metric_id=? "
            "ORDER BY fact_id LIMIT 1",
            [company, period, metric_id],
        ).fetchone()
    if row is None:
        return None
    return {
        "fact_id": int(row[0]), "company": row[1], "period": row[2], "metric_id": row[3],
        "value": float(row[4]), "unit": row[5],
        "raw_value": None if row[6] is None else float(row[6]),
        "raw_unit": row[7],
        "doc": row[8], "page": int(row[9]),
    }


_MONETARY_UNITS = {"元", "千元", "万元", "百万元", "亿元"}


def _raw_note(item: dict) -> str:
    """原文数值/单位与归一化不同时，补一句可核对的原文。

    只在“金额对金额”时补：比率（%）和每股指标（元/股）的原文单位常被
    解析器记成“元/百万元”，补出来反而误导。
    """
    raw_value, raw_unit = item.get("raw_value"), item.get("raw_unit")
    if raw_value is None or not raw_unit or raw_unit == item.get("unit"):
        return ""
    if item.get("unit") not in _MONETARY_UNITS or raw_unit not in _MONETARY_UNITS:
        return ""
    return f"（原文 {raw_value:,.2f} {raw_unit}）"


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
        "raw_value": item.get("raw_value"),
        "raw_unit": item.get("raw_unit"),
    }


def _refusal(question: str, text: str, data: dict | None = None) -> dict:
    return {
        "route": "fact", "question": question, "refused": True, "text": text,
        "data": data or {}, "ledger": [], "citations": [],
    }


def answer(question: str) -> dict | None:
    """回答事实类问题；抽不出槽位或查不到数据时返回 None（交给其它通道）。"""
    slot = extract(question)
    if slot.unresolved:
        words = "、".join(slot.unresolved)
        return _refusal(
            question,
            f"问题里的时间限定「{words}」没能完整解析。为避免答错期间，暂不回答；"
            "请换成明确的说法（如「2025 年第一季度」「2025 年上半年」「2025 年全年」）。",
            {"unresolved": slot.unresolved},
        )
    if slot.metric is None or not slot.companies:
        return None
    metric = slot.metric
    if slot.operation == "yoy" and len(slot.periods) == 1:
        # 同比只给了一个期间：自动补上年同期（如 2026H1 → 2025H1）。
        period = slot.periods[0]
        year = int(period[:4])
        slot.periods.append(f"{year - 1}{period[4:]}")
    import duckdb

    con = duckdb.connect(str(FACT_DB), read_only=True)
    try:
        rows: list[dict] = []
        for company, company_name in zip(slot.companies, slot.company_names):
            for period in slot.periods:
                item = _fetch(con, company, period, metric.metric_id)
                if item is None:
                    return None
                item["company_name"] = company_name
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
        if len(rows) == 1:
            item = rows[0]
            value = item["value"]
            text = (
                f"{item['company_name']} {_period_label(item['period'])}的{name}为 "
                f"{{{{c1}}}} {unit}{_raw_note(item)}。"
            )
        else:
            # “A 和 B 分别多少”“一季报和半年报分别多少”：逐项列出，不再只答第一项。
            parts = []
            for index, item in enumerate(rows, start=1):
                parts.append(
                    f"{item['company_name']} {_period_label(item['period'])}的{name}为 "
                    f"{{{{c{index}}}}} {item['unit']}{_raw_note(item)}"
                )
            value = None
            text = "；".join(parts) + "。"
    elif slot.operation in ("diff_add", "diff_sub"):
        if len(rows) < 2:
            return None
        current, prior = rows[0], rows[1]
        value = current["value"] - prior["value"] if slot.operation == "diff_add" else prior["value"] - current["value"]
        entry = derived(len(ledger) + 1, value, f"{'c1-c2' if slot.operation == 'diff_add' else 'c2-c1'}", unit)
        word = "增加" if slot.operation == "diff_add" else "减少"
        text = (
            f"{slot.company_names[0]} {_period_label(current['period'])}的{name}为 {{{{c1}}}} {unit}，"
            f"{_period_label(prior['period'])}为 {{{{c2}}}} {unit}，{word} {{{{c3}}}} {unit}。"
        )
        assert entry["id"] == "c3"
    elif slot.operation == "yoy":
        if len(rows) < 2:
            return None
        current, prior = rows[0], rows[1]
        value = (current["value"] - prior["value"]) / prior["value"] * 100
        derived(len(ledger) + 1, value, "(c1-c2)/c2*100", "%")
        text = (
            f"{slot.company_names[0]} {_period_label(current['period'])}的{name}为 {{{{c1}}}} {unit}，"
            f"{_period_label(prior['period'])}为 {{{{c2}}}} {unit}，同比增速 {{{{c3}}}}%。"
        )
        unit = "%"
    elif slot.operation == "sum":
        value = sum(item["value"] for item in rows)
        derived(len(ledger) + 1, value, "sum(c*)", unit)
        refs = "、".join(f"{{{{c{i + 1}}}}}" for i in range(len(rows)))
        text = f"{companies} {_period_label(slot.periods[0])}的{name}合计为 {{{{c{len(rows) + 1}}}}} {unit}（{refs} 相加）。"
    elif slot.operation == "range":
        values = [item["value"] for item in rows]
        value = max(values) - min(values)
        derived(len(ledger) + 1, value, "max(c*)-min(c*)", unit)
        refs = "、".join(f"{{{{c{i + 1}}}}}" for i in range(len(rows)))
        text = f"{companies} {_period_label(slot.periods[0])}的{name}最高值比最低值多 {{{{c{len(rows) + 1}}}}} {unit}（{refs}）。"
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
        "data": {"notes": slot.notes, "period_spans": slot.period_spans},
    }
