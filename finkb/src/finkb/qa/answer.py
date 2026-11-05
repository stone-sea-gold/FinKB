"""三路回答：事实通道 / 本体+关系通道 / 原文混合通道。

MVP 只做抽取式回答（不调 LLM）：事实通道输出账本，本体通道输出成员/边，
原文通道输出 Top 块片段与 ``[Ref: doc#P]``；数字一律走账本占位符。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb

from finkb.fact_engine.selector import FINKB_ROOT
from finkb.ontology.operators import note_drill_down
from finkb.qa import fact_lane

FACT_DB = FINKB_ROOT / "data" / "facts.duckdb"
SCOPE_PATH = FINKB_ROOT / "config" / "wiki_scope.yaml"

CLASS_ALIASES = {
    "finkb:HighLeverage": ("资产负债率超过", "高杠杆", "资产负债率高", "负债率超过"),
    "finkb:CashflowDivergence": ("现金流背离", "净利润为正但经营现金流"),
    "finkb:NegativeOcf": ("经营现金流为负", "经营活动现金流为负", "经营现金流净额为负"),
    "finkb:RdIntensive": ("研发强度", "研发投入占", "研发密集"),
    "finkb:LowRoe": ("净资产收益率低", "ROE 低", "ROE低", "低ROE"),
    "finkb:CashRich": ("现金充裕", "现金占比高"),
    "finkb:ReceivableHeavy": ("应收账款高", "应收账款占比高"),
    "finkb:InventoryHeavy": ("存货高", "存货占比高"),
}
REL_KEYWORDS = {
    "子公司": "subsidiary_of",
    "股东": "shareholder_of",
    "担保": "guarantees_for",
    "关联方": "related_party_of",
}


def _load_companies() -> dict[str, str]:
    import yaml

    raw = yaml.safe_load(Path(SCOPE_PATH).read_text(encoding="utf-8"))
    return {str(item["company"]): str(item["company_name"]) for item in raw.get("docs", [])}


def _periods(question: str) -> list[str]:
    slot = fact_lane.extract(question)
    return slot.periods


def _class_hits(class_id: str, periods: list[str]) -> list[dict]:
    con = duckdb.connect(str(FACT_DB), read_only=True)
    try:
        sql = "SELECT company, period, evidence_fact_ids, values_json, severity FROM inferred_concepts WHERE class_id=?"
        params: list = [class_id]
        if periods:
            sql += " AND period=?"
            params.append(periods[0])
        rows = con.execute(sql + " ORDER BY company", params).fetchall()
        if not rows:
            return []
        out = []
        for company, period, evidence, values, severity in rows:
            facts = []
            if evidence:
                ids = list(evidence)
                marks = ",".join("?" for _ in ids)
                facts = con.execute(
                    f"SELECT fact_id, source_doc, page_num, metric_id, value_norm FROM facts_dedup WHERE fact_id IN ({marks})",
                    ids,
                ).fetchall()
            out.append({
                "company": company, "period": period, "severity": severity,
                "values": json.loads(values) if values else {},
                "facts": [{"fact_id": int(f[0]), "doc": f[1], "page": int(f[2]), "metric_id": f[3], "value": float(f[4])} for f in facts],
            })
        return out
    finally:
        con.close()


def answer_ontology(question: str) -> dict | None:
    periods = _periods(question)
    companies = _load_companies()
    # 1) 数值定义类
    for class_id, aliases in CLASS_ALIASES.items():
        if any(alias.replace(" ", "") in question.replace(" ", "") for alias in aliases):
            hits = _class_hits(class_id, periods)
            if not hits:
                return {"route": "ontology", "question": question, "refused": True, "text": "没有找到符合条件的公司（可能该期间不在语料内）。", "data": {"class_id": class_id, "members": []}, "ledger": [], "citations": []}
            names = [f"{companies.get(h['company'], h['company'])}（{h['company']}）" for h in hits]
            ledger = [{"id": "c1", "value": len(hits), "unit": "家", "kind": "derived", "formula": "count(members)", "format": "int"}]
            text = f"满足「{class_id.split(':')[-1]}」的公司有 {{{{c1}}}} 家：" + "、".join(names) + "。"
            citations = sorted({f"[Ref: {f['doc']}#P{f['page']}]" for h in hits for f in h["facts"]})
            return {"route": "ontology", "question": question, "text": text, "data": {"class_id": class_id, "members": hits}, "ledger": ledger, "citations": citations}
    # 2) 关系边
    for keyword, rel_type in REL_KEYWORDS.items():
        if keyword not in question:
            continue
        slot = fact_lane.extract(question)
        if not slot.companies:
            return None
        company = slot.companies[0]
        con = duckdb.connect(str(FACT_DB), read_only=True)
        try:
            raw = con.execute(
                "SELECT dst_id, source_doc, page_num FROM relations WHERE rel_type=? AND src_id=? ORDER BY dst_id",
                [rel_type, company],
            ).fetchall()
        finally:
            con.close()
        from finkb.fact_engine.entity_gate import gate_reason

        sentence_tokens = ("是否", "承诺", "履行", "说明", "计划", "原因", "情况", "备注", "序号", "合计", "小计")
        unique: dict[str, tuple] = {}
        for row in raw:
            name = row[0]
            if gate_reason(name) or any(token in name for token in sentence_tokens) or len(name) > 40:
                continue
            unique.setdefault(name, row)
        rows = list(unique.values())
        if not rows:
            return {"route": "ontology", "question": question, "refused": True, "text": f"没有找到 {company} 的「{keyword}」关系边。", "data": {"rel_type": rel_type, "edges": []}, "ledger": [], "citations": []}
        names = [row[0] for row in rows]
        citations = sorted({f"[Ref: {row[1]}#P{int(row[2])}]" for row in rows})
        ledger = [{"id": "c1", "value": len(rows), "unit": "条", "kind": "derived", "formula": "count(distinct dst)", "format": "int"}]
        text = f"{companies.get(company, company)} 的「{keyword}」共 {{{{c1}}}} 条：" + "、".join(names[:15]) + ("等。" if len(names) > 15 else "。")
        return {"route": "ontology", "question": question, "text": text, "data": {"rel_type": rel_type, "edges": [{"dst": n, "doc": d, "page": int(p)} for n, d, p in rows]}, "ledger": ledger, "citations": citations}
    # 3) 附注穿透
    if "附注" in question:
        slot = fact_lane.extract(question)
        if slot.metric and slot.companies and slot.periods:
            con = duckdb.connect(str(FACT_DB), read_only=True)
            try:
                result = note_drill_down(con, slot.companies[0], slot.periods[0], slot.metric.metric_id)
            finally:
                con.close()
            notes = result.get("notes", [])
            pages = [item.get("page_num") for item in notes]
            label = slot.metric.metric_name
            matched = [item for item in notes if label and label in (item.get("section") or "")]
            if matched:
                pages = [max(matched, key=lambda item: item["page_num"])["page_num"]]
            if pages:
                text = f"{companies.get(slot.companies[0], slot.companies[0])} {slot.periods[0]} 年{slot.metric.metric_name}的附注页为 P{pages[0]}。"
                citations = [f"[Ref: {slot.companies[0]}_{slot.periods[0]}_年报#P{pages[0]}]"]
                return {"route": "ontology", "question": question, "text": text, "data": result, "ledger": [], "citations": citations}
    return None


def _snippet(text: str, question: str, width: int = 260) -> str:
    """按命中词扩到句子边界，避免从半句开始截断。"""
    from finkb.search.hybrid import tokenize

    tokens = [token for token in tokenize(question) if len(token) >= 2]
    positions = [index for token in tokens if (index := text.find(token)) >= 0]
    pos = min(positions) if positions else 0
    start = max(pos - 60, 0)
    for separator in ("。", "；", "\n"):
        index = text.rfind(separator, 0, pos)
        if index >= 0:
            start = index + 1
            break
    end = min(start + width, len(text))
    for separator in ("。", "；", "\n"):
        index = text.find(separator, pos, end + 40)
        if index >= 0:
            end = index + 1
            break
    return text[start:end].replace("\n", " ").strip()


REPORT_TYPE_WORDS = (
    ("年度报告", "年报"), ("年报", "年报"),
    ("半年度报告", "半年报"), ("半年报", "半年报"),
    ("一季度报告", "一季报"), ("一季报", "一季报"),
    ("三季度报告", "三季报"), ("三季报", "三季报"),
)


def _report_type(question: str, periods: list[str]) -> str | None:
    """显式报告类型优先；只给年份时默认年报（如“2025 比亚迪现金构成”）。"""
    for word, value in REPORT_TYPE_WORDS:
        if word in question:
            return value
    if len(periods) == 1 and re.fullmatch(r"20\d{2}", periods[0]):
        return "年报"
    return None


def answer_text(question: str, limit: int = 5) -> dict:
    from finkb.search import hybrid

    slot = fact_lane.extract(question)
    base: dict = {}
    if slot.companies:
        base["company"] = slot.companies[0]
    if slot.periods:
        base["period"] = slot.periods[0]
    report_type = _report_type(question, slot.periods)
    attempts = []
    if report_type:
        attempts.append({**base, "report_type": report_type})
    attempts.append(base)
    attempts.append({})
    results: list[dict] = []
    for filters in attempts:
        results = hybrid.search(question, limit=limit, filters=filters or None)
        if results:
            break
    ledger: list[dict] = []
    citations: list[str] = []
    evidence = []
    seen: set[tuple[str, int]] = set()
    for item in results:
        key = (item["doc_id"], int(item["page_num"]))
        if key in seen:
            continue
        seen.add(key)
        evidence.append({
            "chunk_id": item["chunk_id"], "doc_id": item["doc_id"], "page_num": int(item["page_num"]),
            "section": item.get("section", ""), "snippet": _snippet(item["text"], question),
        })
        citations.append(f"[Ref: {item['doc_id']}#P{item['page_num']}]")
        if len(evidence) >= 3:
            break
    if not evidence:
        return {"route": "text", "question": question, "refused": True, "text": "语料中没有检索到相关原文。", "data": {"evidence": []}, "ledger": [], "citations": []}
    parts = [f"{e['doc_id']} 第 {e['page_num']} 页（{e['section']}）：{e['snippet']}" for e in evidence]
    text = "；".join(parts)
    return {"route": "text", "question": question, "text": text, "data": {"evidence": evidence}, "ledger": ledger, "citations": citations}


NUMERIC_INTENT_RE = re.compile(r"(多少|几|增速|合计|最高|最低|相比|增加|减少|占比|比例|亿元|万元|%|％)")
OUT_OF_SCOPE_TOKENS = ("股价", "市值", "市盈率", "股息率", "员工总数", "员工人数", "员工总人数", "在职员工", "港股", "美股")


def _refuse(question: str, reason: str) -> dict:
    return {"route": "fact", "question": question, "refused": True, "text": reason, "data": {}, "ledger": [], "citations": []}


def precheck(question: str) -> dict | None:
    """进入三档前的确定性拒答；返回 None 表示交给 pipeline。"""
    slot = fact_lane.extract(question)
    if any(token in question for token in OUT_OF_SCOPE_TOKENS):
        return _refuse(question, "问题涉及的指标不在本项目语料范围内。")
    aggregate = any(word in question for word in ("哪些", "所有", "各公司", "全部"))
    if NUMERIC_INTENT_RE.search(question) and slot.metric is not None and not slot.companies and not aggregate:
        from finkb.qa.understanding import scope_companies

        names = "、".join(name for name, _ in scope_companies())
        return _refuse(question, f"没有从问题中识别出公司。当前语料包含 {len(scope_companies())} 家公司：{names}。")
    return None


def answer(question: str) -> dict:
    """三档契约的向后兼容入口：返回带 tier/trace/confidence 的超集。"""
    from finkb.qa.pipeline import ask

    return ask(question)
