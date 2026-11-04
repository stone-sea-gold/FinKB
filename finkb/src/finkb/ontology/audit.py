"""链条完整性体检：把「可追溯」从口号变成可枚举的数字。

``check`` 校验的是**数值之间**是否自洽（资产=负债+权益）；本模块校验的是
**每一环是否接得上**——概念有没有登记、祖先链断没断、证据字段齐不齐、
页面有没有引用、派生指标算不算得出来。

两件事不同：一条事实可以数值完全正确，但它的 metric 没登记概念，于是
链条在第 1 跳就断了。这种断点不会让数字变错，只会让「凭什么信这个数」
无从回答——所以必须单独体检。

全部用集合查询做，不做逐条 ``resolve``：1717 条事实逐条解析要几百次
往返，而这里 5 个 SQL 就能答完。
"""

from __future__ import annotations

from typing import Any

import duckdb

# 每一跳一个检查项；顺序即链顺序。
HOPS = ("concept", "ancestry", "constraints", "evidence", "pages", "derived", "inference")


def audit(
    conn: duckdb.DuckDBPyConnection,
    claims_conn: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, Any]:
    """返回逐跳断点数与样例。``claims_conn`` 为 None 时跳过第⑥跳。"""
    out: dict[str, Any] = {"total_facts": 0, "hops": {}, "samples": {}, "concepts": {}}

    total = conn.execute("SELECT count(*) FROM facts_dedup").fetchone()[0]
    out["total_facts"] = total

    # ① 概念：metric 有没有登记进本体。未登记 = 数在库里但不知道它是什么。
    no_concept = conn.execute(
        "SELECT f.metric_id, count(*) AS n FROM facts_dedup f "
        "LEFT JOIN concepts c ON c.metric_id = f.metric_id "
        "WHERE c.concept_id IS NULL GROUP BY 1 ORDER BY n DESC"
    ).fetchall()
    out["hops"]["concept"] = sum(r[1] for r in no_concept)
    out["samples"]["concept"] = [{"metric_id": r[0], "facts": r[1]} for r in no_concept][:10]

    # ② 祖先链：L2/L3 必须至少有一条到 L0 的路径（递归 CTE 一次算完）。
    reach = conn.execute(
        """
        WITH RECURSIVE up(concept_id, root, depth) AS (
            SELECT concept_id, parent_id, 0 FROM concepts
            UNION ALL
            SELECT u.concept_id, c.parent_id, u.depth + 1
            FROM up u JOIN concepts c ON c.concept_id = u.root
            WHERE u.root IS NOT NULL
        )
        SELECT c.concept_id, c.layer FROM concepts c
        WHERE c.layer IN ('L2','L3')
          AND NOT EXISTS (SELECT 1 FROM up u WHERE u.concept_id = c.concept_id
                          AND u.depth > 0 AND u.root IS NULL)
        """
    ).fetchall()
    out["hops"]["ancestry"] = len(reach)
    out["samples"]["ancestry"] = [{"concept_id": r[0], "layer": r[1]} for r in reach][:10]

    # ③ 约束：L2 概念自身及所有祖先上都没有约束，则它是否自洽无从校验。
    no_constraint = conn.execute(
        """
        WITH RECURSIVE up(origin, concept_id) AS (
            SELECT concept_id, concept_id FROM concepts
            UNION ALL
            SELECT u.origin, c.parent_id
            FROM up u JOIN concepts c ON c.concept_id = u.concept_id
            WHERE c.parent_id IS NOT NULL
        ),
        covered AS (
            SELECT DISTINCT u.origin FROM up u
            WHERE u.concept_id IN (SELECT concept_id FROM ontology_constraints)
        )
        SELECT c.concept_id FROM concepts c
        WHERE c.layer = 'L2' AND c.concept_id NOT IN (SELECT origin FROM covered)
        ORDER BY 1
        """
    ).fetchall() if _has_constraints(conn) else []
    out["hops"]["constraints"] = len(no_constraint)
    out["samples"]["constraints"] = [{"concept_id": r[0]} for r in no_constraint][:10]

    # ④ 证据：source_doc / page_num / raw_text_matched 任一缺失即断。
    bad_evidence = conn.execute(
        "SELECT metric_id, count(*) AS n FROM facts_dedup "
        "WHERE source_doc IS NULL OR source_doc = '' "
        "   OR page_num IS NULL "
        "   OR raw_text_matched IS NULL OR raw_text_matched = '' "
        "GROUP BY 1 ORDER BY n DESC"
    ).fetchall()
    out["hops"]["evidence"] = sum(r[1] for r in bad_evidence)
    out["samples"]["evidence"] = [{"metric_id": r[0], "facts": r[1]} for r in bad_evidence][:10]

    # ⑤ 页面：事实没有被任何 L2 页面引用。这不是错误，是「尚未编译」。
    if claims_conn is not None and (tbl := _claims_table(claims_conn)):
        uncited = claims_conn.execute(
            f"SELECT count(*) FROM facts_dedup f WHERE NOT EXISTS ("
            f"  SELECT 1 FROM {tbl} c WHERE c.fact_id = f.fact_id)"
        ).fetchone()[0]
        out["hops"]["pages"] = uncited
        out["samples"]["pages"] = _uncited_by_metric(claims_conn) if uncited else []
    else:
        out["hops"]["pages"] = None
        out["samples"]["pages"] = []

    # ⑥ 派生：每个 L3 概念在多少 (公司,期间) 上算不出来。
    from finkb.ontology import derive

    pairs = conn.execute(
        "SELECT DISTINCT company, period FROM facts_dedup ORDER BY 1, 2"
    ).fetchall()
    l3 = conn.execute(
        "SELECT concept_id FROM concepts WHERE layer = 'L3' ORDER BY concept_id"
    ).fetchall()
    derived_bad: dict[str, list[str]] = {cid: [] for cid, in l3}
    for company, period in pairs:
        values, _errors = derive.compute(conn, company, period)
        for cid in derived_bad:
            if values.get(cid) is None:
                derived_bad[cid].append(f"{company}/{period}")
    out["hops"]["derived"] = sum(len(v) for v in derived_bad.values())
    out["samples"]["derived"] = [{"concept_id": k, "missing": v[:6], "count": len(v)}
                                 for k, v in derived_bad.items() if v][:10]

    # ⑦ 推理血缘：推断结果必须能回答「依据哪条规则、用了哪几条证据」。
    # 尚未跑过 ontology infer 时不算断点（hop=None，门禁跳过）。
    out["hops"]["inference"], out["samples"]["inference"] = _inference_hop(conn)

    out["concepts"] = {
        "total": conn.execute("SELECT count(*) FROM concepts").fetchone()[0],
        "by_layer": dict(conn.execute(
            "SELECT layer, count(*) FROM concepts GROUP BY 1 ORDER BY 1").fetchall()),
        "l2_without_facts": [r[0] for r in conn.execute(
            "SELECT c.metric_id FROM concepts c WHERE c.layer='L2' AND c.metric_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM facts_dedup f WHERE f.metric_id = c.metric_id)"
        ).fetchall()],
    }
    return out


def _inference_hop(conn: duckdb.DuckDBPyConnection) -> tuple[int | None, list[dict[str, Any]]]:
    """⑦ 推理血缘：每条推断都要有 rule_id 与前提/证据；没跑过推理则返回 None。"""
    try:
        runs = conn.execute("SELECT count(*) FROM inference_runs").fetchone()[0]
    except duckdb.CatalogException:
        return None, []
    if not runs:
        return None, []
    bad_concepts = conn.execute(
        "SELECT class_id, company, period FROM inferred_concepts "
        "WHERE rule_id IS NULL OR rule_id = '' "
        "   OR evidence_fact_ids IS NULL OR len(evidence_fact_ids) = 0"
    ).fetchall()
    bad_edges = conn.execute(
        "SELECT src_id, rel_type, dst_id FROM inferred_edges "
        "WHERE rule_id IS NULL OR rule_id = '' "
        "   OR premises_json IS NULL OR premises_json IN ('', '[]')"
    ).fetchall()
    bad_triples = conn.execute(
        "SELECT count(*) FROM inferred_triples WHERE rule_type IS NULL OR run_id IS NULL"
    ).fetchone()[0]
    samples = ([{"kind": "concept", "detail": f"{r[0]} {r[1]}/{r[2]}"} for r in bad_concepts[:5]]
               + [{"kind": "edge", "detail": f"{r[0]} -{r[1]}-> {r[2]}"} for r in bad_edges[:5]])
    if bad_triples:
        samples.append({"kind": "triple", "detail": f"{bad_triples} 条缺 rule_type/run_id"})
    return len(bad_concepts) + len(bad_edges) + bad_triples, samples


def _has_constraints(conn: duckdb.DuckDBPyConnection) -> bool:
    try:
        conn.execute("SELECT 1 FROM ontology_constraints LIMIT 0")
        return True
    except duckdb.CatalogException:
        return False


def _claims_table(conn: duckdb.DuckDBPyConnection) -> str | None:
    """claims 账本可能在主库，也可能 ATTACH 成 ``wiki`` 库。"""
    for tbl in ("claims", "wiki.claims"):
        try:
            conn.execute(f"SELECT 1 FROM {tbl} LIMIT 0")
            return tbl
        except duckdb.CatalogException:
            continue
    return None


def _uncited_by_metric(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    tbl = _claims_table(conn) or "claims"
    rows = conn.execute(
        f"SELECT f.metric_id, count(*) AS n FROM facts_dedup f WHERE NOT EXISTS ("
        f"  SELECT 1 FROM {tbl} c WHERE c.fact_id = f.fact_id) GROUP BY 1 ORDER BY n DESC"
    ).fetchall()
    return [{"metric_id": r[0], "facts": r[1]} for r in rows][:10]
