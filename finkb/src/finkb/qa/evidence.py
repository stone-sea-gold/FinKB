"""合成子系统的证据包（P1：契约骨架，不接模型、不改现有问答行为）。

设计见 ``docs/合成子系统设计.md``：合成器只消费本模块产出的
:class:`EvidencePack`，不再自己查库。证据分四类：

- ``facts``：结构化事实，来自 fact 通道的 ``ledger``（数字已由 SQL 算好）；
- ``inferences``：本体推断/关系/附注结果，带规则 ID；
- ``passages``：原文块，来自 document 通道的检索结果；
- ``conflicts``：同指标同期间但数值不同的多口径。

本模块是纯函数：``build_pack(question, envelope)`` 接收 pipeline 的
回答信封，返回可序列化的证据包；不读 DuckDB、不调 LLM、不联网。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

from finkb.fact_engine.metric_dict import load_metric_dict

DEFAULT_BUDGET: dict[str, int] = {
    "max_facts": 8,
    "max_inferences": 4,
    "max_passages": 5,
    "max_chars": 4000,
}

_ROUTE_TO_TIER = {"fact": "metric", "ontology": "ontology", "text": "document"}
_TOLERANCE = 1e-6


@dataclass(frozen=True)
class EvidenceFact:
    """一条结构化事实（含派生值与并列口径）。"""

    id: str
    value: float
    unit: str = ""
    metric_id: str = ""
    metric_name: str = ""
    company: str = ""
    period: str = ""
    doc: str = ""
    page: int = 0
    role: str = ""
    kind: str = "fact"  # fact | derived | alternative
    formula: str = ""
    raw_value: float | None = None
    raw_unit: str = ""
    report_date: str | None = None
    format: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "value": self.value,
            "unit": self.unit,
            "metric_id": self.metric_id,
            "metric_name": self.metric_name,
            "company": self.company,
            "period": self.period,
            "doc": self.doc,
            "page": self.page,
            "role": self.role,
            "kind": self.kind,
            "formula": self.formula,
            "raw_value": self.raw_value,
            "raw_unit": self.raw_unit,
            "report_date": self.report_date,
            "format": self.format,
        }


@dataclass(frozen=True)
class EvidenceInference:
    """一条本体推断/关系/附注结果。"""

    id: str
    kind: str  # class | relation | note
    rule_id: str = ""
    text: str = ""
    evidence_ids: tuple[str, ...] = ()
    doc: str = ""
    page: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "rule_id": self.rule_id,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
            "doc": self.doc,
            "page": self.page,
        }


@dataclass(frozen=True)
class EvidencePassage:
    """一段原文（正文或表格）。"""

    id: str
    doc: str
    page: int
    section: str = ""
    block_type: str = "text"  # text | table
    text: str = ""
    chunk_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "doc": self.doc,
            "page": self.page,
            "section": self.section,
            "block_type": self.block_type,
            "text": self.text,
            "chunk_id": self.chunk_id,
        }


@dataclass(frozen=True)
class EvidenceConflict:
    """同指标/公司/期间下数值不一致的多口径事实。"""

    id: str
    metric_id: str
    company: str
    period: str
    fact_ids: tuple[str, ...]
    values: tuple[float, ...]
    docs: tuple[str, ...]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "metric_id": self.metric_id,
            "company": self.company,
            "period": self.period,
            "fact_ids": list(self.fact_ids),
            "values": list(self.values),
            "docs": list(self.docs),
            "note": self.note,
        }


@dataclass
class EvidencePack:
    """一次回答的全部证据，供合成器消费。"""

    question: str
    tier: str = ""
    route: str = ""
    facts: list[EvidenceFact] = field(default_factory=list)
    inferences: list[EvidenceInference] = field(default_factory=list)
    passages: list[EvidencePassage] = field(default_factory=list)
    conflicts: list[EvidenceConflict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    budget: dict[str, int] = field(default_factory=dict)

    def ids(self) -> set[str]:
        out: set[str] = set()
        for group in (self.facts, self.inferences, self.passages, self.conflicts):
            out.update(item.id for item in group)
        return out

    def is_empty(self) -> bool:
        return not (self.facts or self.inferences or self.passages)

    def counts(self) -> dict[str, int]:
        return {
            "facts": len(self.facts),
            "inferences": len(self.inferences),
            "passages": len(self.passages),
            "conflicts": len(self.conflicts),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "tier": self.tier,
            "route": self.route,
            "facts": [item.to_dict() for item in self.facts],
            "inferences": [item.to_dict() for item in self.inferences],
            "passages": [item.to_dict() for item in self.passages],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "notes": list(self.notes),
            "citations": list(self.citations),
            "budget": dict(self.budget),
        }


@lru_cache(maxsize=1)
def _metric_names() -> dict[str, str]:
    try:
        return {metric.metric_id: metric.metric_name for metric in load_metric_dict()}
    except (OSError, ValueError):
        return {}


def _facts_from_ledger(ledger: list[dict[str, Any]]) -> list[EvidenceFact]:
    names = _metric_names()
    out: list[EvidenceFact] = []
    for entry in ledger:
        if entry.get("value") is None:
            continue
        metric_id = str(entry.get("metric_id") or "")
        out.append(
            EvidenceFact(
                id=str(entry.get("id") or f"c{len(out) + 1}"),
                value=float(entry["value"]),
                unit=str(entry.get("unit") or ""),
                metric_id=metric_id,
                metric_name=names.get(metric_id, metric_id),
                company=str(entry.get("company") or ""),
                period=str(entry.get("period") or ""),
                doc=str(entry.get("doc") or ""),
                page=int(entry.get("page") or 0),
                role=str(entry.get("role") or ""),
                kind=str(entry.get("kind") or "fact"),
                formula=str(entry.get("formula") or ""),
                raw_value=entry.get("raw_value"),
                raw_unit=str(entry.get("raw_unit") or ""),
                report_date=entry.get("report_date"),
                format=str(entry.get("format") or ""),
            )
        )
    return out


def _inferences_from_payload(payload: dict[str, Any]) -> list[EvidenceInference]:
    data = payload.get("data") or {}
    route = payload.get("route")
    if route != "ontology":
        return []
    if data.get("class_id"):
        members = data.get("members") or []
        return [
            EvidenceInference(
                id="i1",
                kind="class",
                rule_id=str(data["class_id"]),
                text=f"定义类 {data['class_id']} 命中 {len(members)} 家公司。",
            )
        ]
    if data.get("rel_type"):
        edges = data.get("edges") or []
        return [
            EvidenceInference(
                id="i1",
                kind="relation",
                rule_id=str(data["rel_type"]),
                text=f"{data['rel_type']} 关系 {len(edges)} 条。",
            )
        ]
    if data.get("metric_id"):
        return [
            EvidenceInference(
                id="i1",
                kind="note",
                rule_id="note_drill_down",
                text=str(payload.get("text") or "附注穿透结果。"),
            )
        ]
    return []


def _is_table_snippet(snippet: str) -> bool:
    return any(line.strip().startswith("|") for line in snippet.splitlines()[:3])


def _passages_from_payload(payload: dict[str, Any]) -> list[EvidencePassage]:
    if payload.get("route") != "text":
        return []
    evidence = (payload.get("data") or {}).get("evidence") or []
    out: list[EvidencePassage] = []
    for index, item in enumerate(evidence, start=1):
        snippet = str(item.get("snippet") or "")
        out.append(
            EvidencePassage(
                id=f"e{index}",
                doc=str(item.get("doc_id") or ""),
                page=int(item.get("page_num") or 0),
                section=str(item.get("section") or ""),
                block_type=str(item.get("block_type") or ("table" if _is_table_snippet(snippet) else "text")),
                text=snippet,
                chunk_id=str(item.get("chunk_id") or ""),
            )
        )
    return out


def _conflicts_from_facts(facts: list[EvidenceFact]) -> list[EvidenceConflict]:
    groups: dict[tuple[str, str, str], list[EvidenceFact]] = {}
    for fact in facts:
        if fact.kind != "fact" or not fact.metric_id:
            continue
        groups.setdefault((fact.metric_id, fact.company, fact.period), []).append(fact)
    out: list[EvidenceConflict] = []
    for (metric_id, company, period), items in groups.items():
        values = [item.value for item in items]
        if len(values) < 2:
            continue
        if max(values) - min(values) <= _TOLERANCE * max(1.0, max(abs(value) for value in values)):
            continue
        out.append(
            EvidenceConflict(
                id=f"x{len(out) + 1}",
                metric_id=metric_id,
                company=company,
                period=period,
                fact_ids=tuple(item.id for item in items),
                values=tuple(values),
                docs=tuple(item.doc for item in items),
                note="同一指标同期存在不同数值，需说明口径。",
            )
        )
    return out


def from_envelope(question: str, envelope: dict[str, Any]) -> EvidencePack:
    """把 pipeline 回答信封转成证据包（不裁剪）。"""
    route = str(envelope.get("route") or "")
    tier = str(envelope.get("tier") or _ROUTE_TO_TIER.get(route, ""))
    ledger = list(envelope.get("ledger") or [])
    facts = _facts_from_ledger(ledger)
    data = envelope.get("data") or {}
    notes = [str(note) for note in (data.get("notes") or []) if note]
    if envelope.get("refused"):
        notes.append(str(envelope.get("text") or "拒答"))
    return EvidencePack(
        question=question,
        tier=tier,
        route=route,
        facts=facts,
        inferences=_inferences_from_payload(envelope),
        passages=_passages_from_payload(envelope),
        conflicts=_conflicts_from_facts(facts),
        notes=notes,
        citations=[str(c) for c in (envelope.get("citations") or [])],
    )


def _fact_chars(fact: EvidenceFact) -> int:
    return len(str(fact.value)) + len(fact.unit) + len(fact.formula)


def _total_chars(pack: EvidencePack) -> int:
    return sum(_fact_chars(fact) for fact in pack.facts) + sum(
        len(passage.text) for passage in pack.passages
    ) + sum(len(inference.text) for inference in pack.inferences)


def apply_budget(pack: EvidencePack, budget: dict[str, int] | None = None) -> EvidencePack:
    """按条数与字符预算裁剪证据，裁剪时在 notes 里留痕。"""
    limits = dict(DEFAULT_BUDGET)
    if budget:
        limits.update(budget)
    facts = list(pack.facts[: limits["max_facts"]])
    inferences = list(pack.inferences[: limits["max_inferences"]])
    passages = list(pack.passages[: limits["max_passages"]])
    notes = list(pack.notes)
    trimmed = (
        len(pack.facts) - len(facts)
        + len(pack.inferences) - len(inferences)
        + len(pack.passages) - len(passages)
    )
    while _total_chars(replace(pack, facts=facts, inferences=inferences, passages=passages)) > limits["max_chars"]:
        if passages:
            passages.pop()
            trimmed += 1
        elif inferences:
            inferences.pop()
            trimmed += 1
        elif facts:
            facts.pop()
            trimmed += 1
        else:
            break
    if trimmed:
        notes.append(f"已按预算裁剪 {trimmed} 条证据。")
    return replace(pack, facts=facts, inferences=inferences, passages=passages, notes=notes, budget=limits)


def build_pack(
    question: str,
    envelope: dict[str, Any],
    budget: dict[str, int] | None = None,
) -> EvidencePack:
    """构造并裁剪证据包（P1 对外主入口）。"""
    return apply_budget(from_envelope(question, envelope), budget)
