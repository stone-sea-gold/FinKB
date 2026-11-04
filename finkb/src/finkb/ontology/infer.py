"""确定性推理与带血缘的物化（ADR-057 / ADR-058）。

三类推理，口径分开、结果分表：

1. **数值定义类**（``inferred_concepts``）：OWL 2 RL 没有算术，由
   ``config/ontology_axioms.yaml`` 的 expression 用确定性算子求值；每条命中都记
   ``evidence_fact_ids``，可以一路走回页码。
2. **属性链 / 逆属性 / 对称属性**（``inferred_edges``）：在关系边表上做闭包，
   记 ``premises_json``（用了哪几条基础边）。这一步不依赖 OWL 运行时。
3. **OWL 2 RL 闭包**（``inferred_triples`` + ``inferred.ttl``）：把 TBox/ABox 导出成
   Turtle，跑 ``owlrl`` 前向闭包，只把「基线里没有的三元组」落库。OWL 负责类层级、
   逆属性、传递属性、属性链这些**符号可证**的部分；数值规则不混进来。

为什么批量预计算而不是查询时推理：14,208 三元组的闭包约 6 秒，查询时做会把
问答延迟从毫秒级拖到秒级；推理结果按 run 物化，问答只读结果表。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from finkb.ontology import expr, export, loader, model


def _replace_rows(
    conn: duckdb.DuckDBPyConnection,
    delete_sql: str,
    insert_sql: str,
    rows: list[tuple],
) -> None:
    """删旧写新放在一个事务里。

    DuckDB 默认每条语句各自提交；在这台机器上单条提交有几十毫秒的 fsync 成本，
    几千条推断结果逐条提交会把一次推理拖到分钟级。一个事务只提交一次。
    """
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(delete_sql)
        if rows:
            conn.executemany(insert_sql, rows)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _pivot_facts(conn: duckdb.DuckDBPyConnection) -> tuple[dict, dict]:
    """(公司,期间) → {指标: 值}，以及 (公司,期间) → {指标: [fact_id,...]}。

    同一 (公司,期间,指标) 有多条事实（多份文档）时，取 fact_id 最小的一条做判定值，
    但证据列表保留全部——判定用哪个值必须可复现，证据不能只留一条。
    """
    values: dict[tuple[str, str], dict[str, float]] = {}
    evidence: dict[tuple[str, str], dict[str, list[int]]] = {}
    rows = conn.execute(
        "SELECT company, period, metric_id, value_norm, fact_id FROM facts_dedup "
        "ORDER BY company, period, metric_id, fact_id"
    ).fetchall()
    for company, period, metric_id, value, fact_id in rows:
        key = (company, period)
        env = values.setdefault(key, {})
        if value is not None and metric_id not in env:
            env[metric_id] = float(value)
        evidence.setdefault(key, {}).setdefault(metric_id, []).append(int(fact_id))
    return values, evidence


def materialize_defined_classes(
    conn: duckdb.DuckDBPyConnection,
    ax: model.AxiomDoc,
    run_id: str,
) -> dict[str, Any]:
    """对每个 (公司,期间) 求值数值定义类，写 ``inferred_concepts``。"""
    values, evidence = _pivot_facts(conn)
    hits: list[tuple] = []
    per_class: dict[str, int] = {}
    skipped: dict[str, int] = {}

    for d in ax.defined_classes:
        node = expr.parse_condition(d.expression)
        deps = sorted(expr.condition_deps(node))
        n_hit = 0
        n_skip = 0
        for (company, period), env in sorted(values.items()):
            if any(env.get(m) is None for m in deps):
                n_skip += 1
                continue
            try:
                ok = expr.evaluate_condition(node, env, tol=0.0)
            except expr.ExprError:
                n_skip += 1
                continue
            if not ok:
                continue
            fact_ids = sorted({fid for m in deps for fid in evidence[(company, period)].get(m, [])})
            used = {m: env[m] for m in deps}
            hits.append((
                company, period, d.class_id, d.class_id, d.expression,
                fact_ids, json.dumps(used, ensure_ascii=False), d.severity,
                run_id, _now(),
            ))
            n_hit += 1
        per_class[d.class_id] = n_hit
        skipped[d.class_id] = n_skip

    _replace_rows(
        conn,
        "DELETE FROM inferred_concepts",
        "INSERT INTO inferred_concepts (company, period, class_id, rule_id, expression, "
        "evidence_fact_ids, values_json, severity, run_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        hits,
    )
    return {"per_class": per_class, "hits": len(hits), "skipped": skipped}


def _entity_key(kind: str, ident: str) -> str:
    return f"{kind}:{ident}"


def _base_edges(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT src_type, src_id, rel_type, dst_type, dst_id, source_doc, page_num "
        "FROM relations ORDER BY src_id, rel_type, dst_id, source_doc, page_num"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for src_type, src_id, rel_type, dst_type, dst_id, source_doc, page_num in rows:
        prop = export.REL_TO_PROPERTY.get(rel_type)
        if prop is None:
            continue
        out.append({
            "src": _entity_key(src_type, src_id),
            "prop": prop,
            "dst": _entity_key(dst_type, dst_id),
            "source_doc": source_doc,
            "page_num": page_num,
        })
    return out


def materialize_edges(
    conn: duckdb.DuckDBPyConnection,
    ax: model.AxiomDoc,
    run_id: str,
) -> dict[str, Any]:
    """在关系边表上做逆属性 / 对称属性 / 属性链闭包，写 ``inferred_edges``。"""
    base = _base_edges(conn)
    inferred: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def add(src: str, prop: str, dst: str, rule_id: str, premises: list[dict[str, Any]],
            source_doc: str | None = None, page_num: int | None = None) -> None:
        key = (src, prop, dst, rule_id)
        if key in inferred:
            return
        inferred[key] = {
            "src": src, "prop": prop, "dst": dst, "rule_id": rule_id,
            "premises_json": json.dumps(premises, ensure_ascii=False),
            "source_doc": source_doc, "page_num": page_num,
        }

    by_prop: dict[str, list[dict[str, Any]]] = {}
    for e in base:
        by_prop.setdefault(e["prop"], []).append(e)

    for prop, edges in by_prop.items():
        p = ax.property_by_id(prop)
        if not p:
            continue
        if p.inverse_of:
            for e in edges:
                add(e["dst"], p.inverse_of, e["src"], f"inverse:{prop}",
                    [e], e["source_doc"], e["page_num"])
        if "symmetric" in p.characteristics:
            for e in edges:
                add(e["dst"], prop, e["src"], f"symmetric:{prop}",
                    [e], e["source_doc"], e["page_num"])

    # 属性链：A --p1--> B，B --p2--> C ⇒ A --implies--> C
    # 用 base + 已推出的边一起做，支持链条长度 > 2。
    for ch in ax.property_chains:
        changed = True
        # 理论上链条闭包最多迭代「新边数」轮；给个硬上限，防止规则写错成环时死循环。
        for _round in range(10):
            if not changed:
                break
            changed = False
            index: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for e in base + [dict(v) for v in inferred.values()]:
                index.setdefault((e["src"], e["prop"]), []).append(e)
            for e1 in list(base + [dict(v) for v in inferred.values()]):
                if e1["prop"] != ch.properties[0]:
                    continue
                for e2 in index.get((e1["dst"], ch.properties[1]), []):
                    if e2["dst"] == e1["src"]:
                        continue
                    before = len(inferred)
                    add(e1["src"], ch.implies, e2["dst"], f"chain:{ch.chain_id}",
                        [e1, e2], e1["source_doc"], e1["page_num"])
                    changed = changed or len(inferred) > before
        else:
            # 到上限仍有新边，说明链条可能自环；保留已推结果并告警。
            ax.warnings.append(f"属性链 {ch.chain_id} 迭代 10 轮仍未收敛，已截断")

    rows = list(inferred.values())
    _replace_rows(
        conn,
        "DELETE FROM inferred_edges",
        "INSERT INTO inferred_edges (src_id, rel_type, dst_id, rule_id, premises_json, "
        "source_doc, page_num, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (r["src"], r["prop"], r["dst"], r["rule_id"],
             r["premises_json"], r["source_doc"], r["page_num"], run_id, _now())
            for r in rows
        ],
    )
    return {"base_edges": len(base), "inferred_edges": len(rows),
            "by_rule": _count_by_rule(rows)}


def _count_by_rule(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["rule_id"]] = out.get(r["rule_id"], 0) + 1
    return dict(sorted(out.items()))


OWL = "http://www.w3.org/2002/07/owl#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


def _is_trivial_triple(s: Any, p: Any, o: Any) -> bool:
    """过滤 OWL RL 的恒真噪音：自反 sameAs、所有个体都是 owl:Thing。

    这两类三元组占推断量的大头（实测 5,998 条里约 2,500 条自反 sameAs），
    对问答没有信息量，落库只会把表撑大。
    """
    if str(p) == OWL + "sameAs" and str(s) == str(o):
        return True
    return str(p) == RDF + "type" and str(o) == OWL + "Thing"


def run_owlrl(
    conn: duckdb.DuckDBPyConnection,
    doc: model.OntologyDoc,
    ax: model.AxiomDoc,
    out_dir: str | Path,
    run_id: str,
    *,
    scope_path: str | Path | None = None,
) -> dict[str, Any]:
    """导出图 → OWL 2 RL 前向闭包 → 只落「基线之外」的三元组。"""
    from owlrl import DeductiveClosure, OWLRL_Semantics
    from rdflib import Graph

    t0 = time.perf_counter()
    tbox = export.build_tbox(doc, ax)
    abox = export.build_abox(conn, doc, ax, scope_path=scope_path,
                             with_relation_provenance=False)
    g = Graph()
    for triple in tbox:
        g.add(triple)
    for triple in abox:
        g.add(triple)
    base = set(g)
    DeductiveClosure(OWLRL_Semantics).expand(g)
    inferred = {t for t in (set(g) - base) if not _is_trivial_triple(*t)}
    elapsed = time.perf_counter() - t0

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ig = Graph()
    for s, p, o in inferred:
        ig.add((s, p, o))
    tmp = out / "inferred.ttl.tmp"
    tmp.write_text(ig.serialize(format="turtle"), encoding="utf-8")
    tmp.replace(out / "inferred.ttl")

    _replace_rows(
        conn,
        "DELETE FROM inferred_triples",
        "INSERT INTO inferred_triples (subject, predicate, object, rule_type, run_id) "
        "VALUES (?,?,?,?,?)",
        [(str(s), str(p), str(o), "owlrl", run_id) for s, p, o in inferred],
    )
    return {
        "tbox_triples": len(tbox),
        "abox_triples": len(abox),
        "base_triples": len(base),
        "inferred_triples": len(inferred),
        "elapsed_s": round(elapsed, 3),
        "out_file": str(out / "inferred.ttl"),
    }


def run(
    db_path: str | Path,
    out_dir: str | Path = "data/ontology",
    *,
    ontology_path: str | Path | None = None,
    axioms_path: str | Path | None = None,
    skip_owl: bool = False,
    scope_path: str | Path | None = None,
) -> dict[str, Any]:
    """跑完整推理流水线，返回报告并写 ``inference_report.json``。"""
    started = _now()
    run_id = _new_run_id()
    doc = model.load(ontology_path)
    ax = model.load_axioms(axioms_path, doc=doc)

    conn = duckdb.connect(str(db_path))
    try:
        loader.ensure_schema(conn)
        defined = materialize_defined_classes(conn, ax, run_id)
        edges = materialize_edges(conn, ax, run_id)
        owl = {"skipped": True}
        if not skip_owl:
            owl = run_owlrl(conn, doc, ax, out_dir, run_id, scope_path=scope_path)
        finished = _now()
        report = {
            "run_id": run_id,
            "started_at": started,
            "finished_at": finished,
            "defined_classes": defined,
            "edges": edges,
            "owlrl": owl,
            "warnings": list(ax.warnings),
        }
        conn.execute(
            "INSERT OR REPLACE INTO inference_runs (run_id, started_at, finished_at, "
            "tbox_triples, abox_triples, inferred_triples, defined_hits, chain_edges, "
            "status, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [run_id, started, finished,
             int(owl.get("tbox_triples", 0)), int(owl.get("abox_triples", 0)),
             int(owl.get("inferred_triples", 0)), int(defined["hits"]),
             int(edges["inferred_edges"]), "ok",
             json.dumps({"skip_owl": skip_owl}, ensure_ascii=False)],
        )
    finally:
        conn.close()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "inference_report.json.tmp"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out / "inference_report.json")
    return report
