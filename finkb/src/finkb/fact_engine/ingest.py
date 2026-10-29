"""L1 入库流水线：选表 → LLM 抽 Observation → 逐字核对 → 写 facts / unmatched_pool。

两个刻意的取舍：

- **同一文档内同一 (期间, 指标) 只留一条**：``facts`` 的唯一键含 ``source_doc``，
  而主要会计数据和利润表都会披露营业收入；按 summary → 资产负债表 → 利润表 →
  现金流量表的顺序抽，先到先得，后面的重复项直接跳过（不是失败，不进 unmatched）。
- **单位按公司推断**：同一家公司所有报表用同一个记账单位（元/千元/万元），
  用「主要会计数据的营业收入」和「利润表的营业收入」数量级比一下即可确定；
  没有主要会计数据的文档（少数半年报）沿用同公司其他文档的结论。

并发只在「抽取」段：DuckDB 的连接不是线程安全的，写库全部回到主线程。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from finkb.fact_engine.extractor import LLMClient, extract_table
from finkb.fact_engine.metric_dict import UNIT_DERIVATION, Metric
from finkb.fact_engine.selector import SourceTable, load_tables, select_tables
from finkb.fact_engine.store import FactStore
from finkb.fact_engine.verifier import parse_number, verify

UNIT_CHOICES = {"元": 1.0, "千元": 1e3, "万元": 1e4}
_CALIBER = "合并报表"

Extraction = tuple[SourceTable, list[dict], dict[int, str]]


def _revenue_values(table: SourceTable) -> list[float]:
    """取「营业收入/营业总收入」行里所有非空数值（跳过附注/行次列）。"""
    out: list[float] = []
    for row in table.rows:
        label = re.sub(r"[\s\u3000（）()：:，,]", "", str(row[0])).lstrip("一").lstrip("、")
        if label.startswith(("营业收入", "营业总收入", "其中营业收入")):
            for j, cell in enumerate(row[1:], start=1):
                head = table.header[j] if j < len(table.header) else ""
                if "附注" in head or "行次" in head or "序号" in head:
                    continue
                value = parse_number(cell)
                if value is not None:
                    out.append(value)
            break
    return out


def _revenue_of(table: SourceTable) -> float | None:
    """表里「营业收入」的最大一列（三季报要取「年初至」累计，而不是单季）。"""
    values = _revenue_values(table)
    return max(values) if values else None


def infer_unit(tables: list[SourceTable]) -> str | None:
    """用 summary 与利润表的营业收入比数量级，判定记账单位。"""
    summary = next((t for t in tables if t.kind == "summary"), None)
    income = next((t for t in tables if t.kind == "is"), None)
    if not summary or not income:
        return None
    a, b = _revenue_of(summary), _revenue_of(income)
    if not a or not b:
        return None
    ratio = b / a
    best = min(UNIT_CHOICES, key=lambda u: abs(ratio - 1 / UNIT_CHOICES[u]))
    expected = 1 / UNIT_CHOICES[best]
    return best if abs(ratio - expected) / expected < 0.2 else None


def company_units(docs: list[Path], manifest: dict) -> dict[str, str]:
    """每家公司取第一份能判定的文档推断单位（年报通常在列表最前）。"""
    out: dict[str, str] = {}
    ordered = sorted(docs, key=lambda d: (not d.name.endswith("年报"), d.name))
    for doc in ordered:
        code = doc.name.split("_")[0]
        if code in out:
            continue
        unit = infer_unit(select_tables(load_tables(doc, manifest)))
        if unit:
            out[code] = unit
    return out


def extract_document(
    client: LLMClient, doc_dir: Path, metrics: list[Metric], manifest: dict
) -> list[Extraction]:
    """纯网络段：抽一份文档的所有选中表，不碰数据库。"""
    out: list[Extraction] = []
    for table in select_tables(load_tables(doc_dir, manifest)):
        obs, periods = extract_table(client, table, metrics)
        out.append((table, obs, periods))
    return out


def write_document(
    store: FactStore, extracted: list[Extraction], metrics: list[Metric], unit: str
) -> dict[str, int]:
    """把一份文档的抽取结果写库；返回计数。"""
    stats = {"tables": len(extracted), "observations": 0, "facts": 0, "unmatched": 0, "duplicated": 0}
    by_id = {m.metric_id: m for m in metrics}
    seen: set[tuple[str, str]] = set()
    for table, obs, periods in extracted:
        # 主要会计数据一律按「元」记账（利润表可能是千元/万元），不能套公司报表单位
        table_unit = "元" if table.kind == "summary" else unit
        stats["observations"] += len(obs)
        for item in obs:
            verified, reason = verify(item, table, by_id, periods, table_unit)
            if verified is None:
                store.add_unmatched({"table": table.logical_id, "obs": item}, reason or "schema_invalid")
                stats["unmatched"] += 1
                continue
            key = (verified.period, verified.metric.metric_id)
            if key in seen:
                stats["duplicated"] += 1
                continue
            seen.add(key)
            store.insert_fact(
                company=table.company,
                period=verified.period,
                metric_id=verified.metric.metric_id,
                unit_family=UNIT_DERIVATION[verified.metric.unit_default][0],
                unit_of_account=table_unit,
                norm_unit=verified.metric.unit_default,
                value_raw=verified.value_raw,
                raw_text_matched=verified.raw_text,
                value_norm=verified.value_norm,
                caliber=_CALIBER,
                reporting_party=table.company_name,
                report_date=date.fromisoformat(table.report_date) if table.report_date else date.today(),
                source_doc=table.doc_id,
                page_num=table.page,
                table_id=table.logical_id,
                match_level=verified.match_level,
            )
            stats["facts"] += 1
    return stats


def ingest_documents(
    store: FactStore,
    client: LLMClient,
    docs: list[Path],
    metrics: list[Metric],
    manifest: dict,
    units: dict[str, str],
    workers: int = 4,
) -> dict[str, dict[str, int]]:
    """并发抽取（网络）、串行写库；返回每份文档的计数。"""
    def run(doc: Path) -> list[Extraction] | str:
        try:
            return extract_document(client, doc, metrics, manifest)
        except Exception as exc:  # 单份文档失败不拖垮整批，错误进结果里
            return f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        extracted = list(pool.map(run, docs))
    results: dict[str, dict[str, int]] = {}
    for doc, items in zip(docs, extracted):
        if isinstance(items, str):
            results[doc.name] = {"error": items}
            continue
        tables = [t for t, _obs, _p in items]
        unit = infer_unit(tables) or units.get(doc.name.split("_")[0], "元")
        results[doc.name] = write_document(store, items, metrics, unit)
    return results
