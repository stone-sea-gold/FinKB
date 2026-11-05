"""三档契约：Tier 1 指标 → Tier 2 本体 → Tier 3 文档，逐级回落（ADR-060）。

与旧版 ``answer()`` 的区别：

- **显式档位**：每个回答都带 ``tier``；``route`` 只保留给旧评测/兼容。
- **能力剪枝**：指标库/本体推断表/切块索引任一缺失时，跳过对应档位。
- **残差门**：Tier 1 命中指标后，如果问句里还有「分地区/海外/客户/渠道」
  这类指标口径回答不了的限定词，主动降级而不是硬答总数。
- **可解释**：``trace`` 记录剪枝、跳过原因和每档命中/未命中，``--explain`` 可看。

数字仍只由 SQL 计算（ADR-001），本模块不生成也不改写任何数值。
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb

from finkb.fact_engine.selector import FINKB_ROOT
from finkb.qa import fact_lane, understanding
from finkb.qa.router import route as router_route

FACT_DB = FINKB_ROOT / "data" / "facts.duckdb"
CHUNKS_DB = FINKB_ROOT / "data" / "_search" / "chunks.db"

TIERS = ("metric", "ontology", "document")
TIER_LABELS = {"metric": "Tier 1 指标", "ontology": "Tier 2 本体", "document": "Tier 3 文档"}
ROUTE_BY_TIER = {"metric": "fact", "ontology": "ontology", "document": "text"}
CONFIDENCE = {"metric": 0.95, "ontology": 0.85, "document": 0.7}

def capabilities() -> dict[str, bool]:
    """探测三档的数据可用性；任何异常都按「不可用」处理，不阻塞其它档。"""
    caps = {"metric": False, "ontology": False, "document": False}
    if FACT_DB.exists():
        try:
            con = duckdb.connect(str(FACT_DB), read_only=True)
            try:
                caps["metric"] = con.execute("SELECT COUNT(*) FROM facts_dedup").fetchone()[0] > 0
            except duckdb.Error:
                caps["metric"] = False
            try:
                caps["ontology"] = con.execute("SELECT COUNT(*) FROM inferred_concepts").fetchone()[0] > 0
            except duckdb.Error:
                caps["ontology"] = False
            finally:
                con.close()
        except Exception:  # noqa: BLE001 - 探测失败按不可用处理
            pass
    caps["document"] = CHUNKS_DB.exists() and CHUNKS_DB.stat().st_size > 0
    return caps


def residual_qualifiers(question: str) -> list[str]:
    """剥离公司/指标/期间/数字/问句填充词后，仍存在的范围限定词。"""
    return understanding.residual_qualifiers(question)


def precheck(question: str) -> dict | None:
    """进入档位前的确定性拒答；返回 None 表示放行。"""
    from finkb.qa import answer as answer_mod

    return answer_mod.precheck(question)


def _envelope(question: str, tier: str, payload: dict, trace: list[dict], *, confidence: float | None = None) -> dict:
    body = dict(payload)
    body.setdefault("data", {})
    body.setdefault("ledger", [])
    body.setdefault("citations", [])
    refused = bool(body.get("refused"))
    body["question"] = question
    body["tier"] = tier
    body["route"] = ROUTE_BY_TIER[tier]
    body["confidence"] = 0.0 if refused else (confidence if confidence is not None else CONFIDENCE[tier])
    body["trace"] = trace
    return body


def _refusal(question: str, tier: str, text: str, trace: list[dict], *, reason: str) -> dict:
    trace.append({"step": "refuse", "tier": tier, "reason": reason, "detail": text})
    return _envelope(question, tier, {"refused": True, "text": text}, trace, confidence=0.0)


def _plan(question: str, order: list[str], caps: dict[str, bool], trace: list[dict]) -> list[str]:
    """根据能力/路由提示/残差门裁剪档位顺序，并把原因写进 trace。"""
    hint = router_route(question).name
    trace.append({"step": "router_hint", "hint": hint})

    planned = list(order)
    for tier in order:
        if not caps.get(tier):
            planned.remove(tier)
            trace.append({"step": "prune", "tier": tier, "reason": "导线库/索引不可用"})
    if not planned:
        return planned

    if hint == "text":
        planned = [tier for tier in planned if tier == "document"]
        trace.append({"step": "hint", "effect": "text-first", "tiers": planned})
        return planned
    if hint == "ontology":
        planned = [tier for tier in planned if tier != "metric"]
        trace.append({"step": "hint", "effect": "skip-metric", "tiers": planned})
    if "附注" in question and fact_lane.extract(question).metric is not None:
        planned = [tier for tier in planned if tier != "metric"]
        trace.append({"step": "hint", "effect": "note-drill", "tiers": planned})
    residual = residual_qualifiers(question)
    if residual and "metric" in planned:
        planned.remove("metric")
        trace.append({"step": "residual_gate", "status": "downgrade", "qualifiers": residual})
    return planned


def ask(
    question: str,
    *,
    tier: str = "auto",
    explain: bool = False,
    capabilities_override: dict[str, bool] | None = None,
) -> dict:
    """按三档契约回答；``tier`` 可强制指定某一档（不可用时拒答）。"""
    if tier != "auto" and tier not in TIERS:
        raise ValueError(f"tier 只能是 auto/{'/'.join(TIERS)}")
    caps = capabilities_override if capabilities_override is not None else capabilities()
    trace: list[dict] = [{"step": "capability", "available": caps}]

    refused = precheck(question)
    if refused is not None:
        return _envelope(question, "metric", refused, trace, confidence=0.0)

    if tier != "auto":
        if not caps.get(tier):
            return _refusal(question, tier, f"{TIER_LABELS[tier]}不可用（数据或索引缺失）。", trace, reason="tier-unavailable")
        order = [tier]
    else:
        order = list(TIERS)

    planned = _plan(question, order, caps, trace)
    if not planned:
        return _refusal(question, "document", "三个通道都不可用：缺少事实库/本体推断/检索索引。", trace, reason="no-capability")

    from finkb.qa import answer as answer_mod

    for attempt in planned:
        if attempt == "metric":
            payload = fact_lane.answer(question)
            if payload is None:
                trace.append({"step": "attempt", "tier": "metric", "status": "miss"})
                # 数值题命中指标但查不到数据：明确拒答，不降级去答别的问题。
                if tier == "auto":
                    return _refusal(
                        question, "metric",
                        "没有找到对应的事实数据（可能指标、公司或期间不在语料内）。",
                        trace, reason="metric-no-data",
                    )
                continue
            trace.append({"step": "attempt", "tier": "metric", "status": "hit"})
            return _envelope(question, "metric", payload, trace)
        if attempt == "ontology":
            payload = answer_mod.answer_ontology(question)
            if payload is None:
                trace.append({"step": "attempt", "tier": "ontology", "status": "miss"})
                continue
            trace.append({"step": "attempt", "tier": "ontology", "status": "hit"})
            return _envelope(question, "ontology", payload, trace)
        payload = answer_mod.answer_text(question)
        trace.append({"step": "attempt", "tier": "document", "status": "hit" if not payload.get("refused") else "refused"})
        return _envelope(question, "document", payload, trace, confidence=0.4 if payload.get("refused") else None)

    return _refusal(question, planned[-1], "没有找到能回答这个问题的证据。", trace, reason="all-miss")
