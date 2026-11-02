"""链条解析：从一个指标一路查到原文页码。（方案 v2.3 §7.5.2 的六跳）

    ① metric_id  → ② concept   → ③ 父概念（上溯到 L0）
                                 → ④ 约束
                 → ⑤ fact      → ⑥ 证据（文档/页码/表格）
                               → ⑦ 页面（claims 账本）

每一跳都可以单独断：概念没配 → 报"概念缺失"；事实没抽到 → 报"无事实"；
证据缺页码 → 报"证据不完整"。**不允许静默返回空结果**——链条断了要看得见。
"""

from __future__ import annotations

from typing import Any

import duckdb

# 链条各跳的名称。给 CLI 的 text 输出用，也给契约测试断言用。
HOPS = ("concept", "ancestry", "constraints", "facts", "evidence", "pages")


def _row_to_dict(cur: duckdb.DuckDBPyConnection, row: tuple, like_table: str) -> dict[str, Any]:
    cols = [d[0] for d in cur.execute(f"SELECT * FROM {like_table} LIMIT 0").description]
    return dict(zip(cols, row))


def concept_of_metric(conn: duckdb.DuckDBPyConnection, metric_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM concepts WHERE metric_id = ?", [metric_id]).fetchone()
    return _row_to_dict(conn, row, "concepts") if row else None


def concept_by_id(conn: duckdb.DuckDBPyConnection, concept_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM concepts WHERE concept_id = ?", [concept_id]).fetchone()
    return _row_to_dict(conn, row, "concepts") if row else None


def ancestry(conn: duckdb.DuckDBPyConnection, concept_id: str) -> list[dict[str, Any]]:
    """自身 → 父 → …… → 根。用递归 CTE，不引图数据库。"""
    rows = conn.execute(
        """
        WITH RECURSIVE up(concept_id, label, layer, parent_id, depth) AS (
            SELECT concept_id, label, layer, parent_id, 0 FROM concepts WHERE concept_id = ?
            UNION ALL
            SELECT c.concept_id, c.label, c.layer, c.parent_id, up.depth + 1
            FROM concepts c JOIN up ON c.concept_id = up.parent_id
        )
        SELECT concept_id, label, layer, parent_id, depth FROM up ORDER BY depth
        """,
        [concept_id],
    ).fetchall()
    return [
        {"concept_id": r[0], "label": r[1], "layer": r[2], "parent_id": r[3], "depth": r[4]}
        for r in rows
    ]


def constraints_of(conn: duckdb.DuckDBPyConnection, concept_id: str) -> list[dict[str, Any]]:
    """概念自身 + 祖先的约束（约束沿 is_a 继承）。"""
    rows = conn.execute(
        """
        WITH RECURSIVE up(concept_id) AS (
            SELECT concept_id FROM concepts WHERE concept_id = ?
            UNION ALL
            SELECT c.parent_id FROM concepts c JOIN up ON c.concept_id = up.concept_id
            WHERE c.parent_id IS NOT NULL
        )
        SELECT constraint_id, concept_id, rule_type, expression, tolerance, note
        FROM ontology_constraints WHERE concept_id IN (SELECT concept_id FROM up)
        ORDER BY constraint_id
        """,
        [concept_id],
    ).fetchall()
    return [
        {"constraint_id": r[0], "concept_id": r[1], "rule_type": r[2],
         "expression": r[3], "tolerance": r[4], "note": r[5]}
        for r in rows
    ]


def relations_of(conn: duckdb.DuckDBPyConnection, concept_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT src_concept, rel_type, dst_concept, attrs_json FROM concept_relations
        WHERE src_concept = ? OR dst_concept = ?
        ORDER BY rel_type, src_concept
        """,
        [concept_id, concept_id],
    ).fetchall()
    return [{"src": r[0], "rel_type": r[1], "dst": r[2], "attrs_json": r[3]} for r in rows]


def facts_of(
    conn: duckdb.DuckDBPyConnection,
    metric_id: str,
    company: str | None = None,
    period: str | None = None,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT fact_id, company, period, metric_id, value_norm, unit_of_account, "
        "norm_unit, currency, caliber, reporting_party, report_date, source_doc, page_num, "
        "table_id, raw_text_matched, match_level, dict_version "
        "FROM facts_dedup WHERE metric_id = ?"
    )
    params: list[Any] = [metric_id]
    if company:
        sql += " AND company = ?"
        params.append(company)
    if period:
        sql += " AND period = ?"
        params.append(period)
    sql += " ORDER BY company, period"
    rows = conn.execute(sql, params).fetchall()
    keys = ("fact_id", "company", "period", "metric_id", "value_norm", "unit_of_account",
            "norm_unit", "currency", "caliber", "reporting_party", "report_date", "source_doc",
            "page_num", "table_id", "raw_text_matched", "match_level", "dict_version")
    return [dict(zip(keys, r)) for r in rows]


def evidence_of(fact: dict[str, Any]) -> dict[str, Any]:
    """第⑤跳：一条事实凭什么可信。

    缺 ``page_num`` 或 ``raw_text_matched`` 即判「证据不完整」——这两个字段是
    「数字只由 SQL 计算」这条不变量能被复核的前提。
    """
    missing = [k for k in ("source_doc", "page_num", "raw_text_matched") if not fact.get(k)]
    return {
        "source_doc": fact.get("source_doc"),
        "page_num": fact.get("page_num"),
        "table_id": fact.get("table_id"),
        "raw_text_matched": fact.get("raw_text_matched"),
        "match_level": fact.get("match_level"),
        "caliber": fact.get("caliber"),
        "report_date": str(fact.get("report_date") or ""),
        "complete": not missing,
        "missing": missing,
    }


def pages_of(conn: duckdb.DuckDBPyConnection, fact_id: int) -> list[dict[str, Any]]:
    """第⑥跳：这条事实出现在哪些 L2 页面里（claims 账本）。

    claims 账本在 ``data/wiki.duckdb``，与 facts 分库；调用方传进来的连接
    可以是 ATTACH 过 wiki 库的主连接，也可以是 ClaimStore 的连接。
    """
    sql = ("SELECT page, ordinal, kind, display_value, anchor FROM {tbl} "
           "WHERE fact_id = ? ORDER BY page, ordinal")
    try:
        rows = conn.execute(sql.format(tbl="claims"), [fact_id]).fetchall()
    except duckdb.CatalogException:
        try:
            # claims 在 data/wiki.duckdb，调用方 ATTACH 成了 wiki 库
            rows = conn.execute(sql.format(tbl="wiki.claims"), [fact_id]).fetchall()
        except duckdb.CatalogException:
            # 只有 facts 的裸库（测试、离线分析）：没有 claims 账本，第⑥跳为空。
            return []
    return [
        {"page": r[0], "ordinal": r[1], "kind": r[2], "display_value": r[3], "anchor": r[4]}
        for r in rows
    ]


def derived_uses(conn: duckdb.DuckDBPyConnection, metric_id: str) -> list[dict[str, Any]]:
    """哪些 L3 派生指标用到它（反向跳，回答"改这个数会影响哪些页面结论"）。"""
    rows = conn.execute(
        "SELECT concept_id, label, formula, expression_args FROM concepts "
        "WHERE layer = 'L3' AND list_contains(expression_args, ?) ORDER BY concept_id",
        [metric_id],
    ).fetchall()
    return [{"concept_id": r[0], "label": r[1], "formula": r[2], "args": r[3]} for r in rows]


def resolve(
    conn: duckdb.DuckDBPyConnection,
    metric_id: str,
    company: str | None = None,
    period: str | None = None,
    with_pages: bool = True,
    claims_conn: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, Any]:
    """解析完整链条。链条断在哪，报告里就写哪。"""
    out: dict[str, Any] = {
        "metric_id": metric_id,
        "company": company,
        "period": period,
        "concept": None,
        "ancestry": [],
        "relations": [],
        "constraints": [],
        "facts": [],
        "derived_uses": [],
        "breaks": [],
    }
    # 既接受 metric_id（L2 常见），也接受 concept_id（L3 派生指标没有 metric_id）。
    concept = concept_of_metric(conn, metric_id) or concept_by_id(conn, metric_id)
    if concept is None:
        out["breaks"].append({
            "hop": "concept",
            "reason": f"{metric_id!r} 既不是已登记概念的 metric_id 也不是 concept_id"
                      "——去 config/ontology.yaml 加一条",
        })
        return out
    out["concept"] = concept
    out["ancestry"] = ancestry(conn, concept["concept_id"])
    out["relations"] = relations_of(conn, concept["concept_id"])
    out["constraints"] = constraints_of(conn, concept["concept_id"])
    out["derived_uses"] = derived_uses(conn, metric_id)
    if concept.get("formula"):
        out["derived"] = _derived_block(conn, concept, company, period)

    facts = facts_of(conn, metric_id, company=company, period=period)
    if not facts and not concept.get("formula"):
        out["breaks"].append({
            "hop": "facts",
            "reason": "库里没有该指标的事实——先跑 L1 抽取，或确认公司/期间拼写",
        })
    for f in facts:
        ev = evidence_of(f)
        if not ev["complete"]:
            out["breaks"].append({
                "hop": "evidence",
                "reason": f"事实 {f['fact_id']} 证据不完整，缺 {ev['missing']}",
            })
        pages = pages_of(claims_conn or conn, f["fact_id"]) if with_pages else []
        if with_pages and not pages:
            out["breaks"].append({
                "hop": "pages",
                "reason": f"事实 {f['fact_id']} 尚未被任何页面引用（L2 还没编译到它）",
            })
        out["facts"].append({
            "fact_id": f["fact_id"],
            "company": f["company"],
            "period": f["period"],
            "value_norm": f["value_norm"],
            "unit": f.get("norm_unit") or f["unit_of_account"],
            "caliber": f["caliber"],
            "dict_version": f["dict_version"],
            "evidence": ev,
            "pages": pages,
        })
    return out


def _derived_block(conn: duckdb.DuckDBPyConnection, concept: dict[str, Any],
                   company: str | None, period: str | None) -> dict[str, Any]:
    """L3 的「第 5 跳」不是一条事实，而是一次按公式的确定性计算。

    参数各自指回自己的 L2 概念与事实条数——改一个底层事实会影响哪个派生值，
    这条链反向可查。
    """
    from finkb.ontology import derive

    values: dict[str, float] = {}
    errors: list[str] = []
    if company and period:
        values, errors = derive.compute(conn, company, period)
    args = []
    for a in concept.get("expression_args") or []:
        cm = concept_of_metric(conn, a) or concept_by_id(conn, a)
        args.append({
            "metric_id": a,
            "concept_id": (cm or {}).get("concept_id"),
            "label": (cm or {}).get("label"),
            "facts": len(facts_of(conn, a, company=company, period=period)),
        })
    return {
        "formula": concept.get("formula"),
        "value": values.get(concept["concept_id"]),
        "unit": concept.get("unit_default"),
        "args": args,
        "errors": [e for e in errors if e.startswith(concept["concept_id"])],
    }


def format_text(chain: dict[str, Any]) -> str:
    """人读版。用于 CLI 与排错。"""
    c = chain["concept"]
    lines = [f"指标 {chain['metric_id']}" + (f"（{chain['company']} / {chain['period']}）"
                                            if chain["company"] or chain["period"] else "")]
    if c is None:
        lines.append("  ✗ 概念缺失")
        for b in chain["breaks"]:
            lines.append(f"    - {b['reason']}")
        return "\n".join(lines)

    lines.append(f"  [1-2] 概念  {c['concept_id']}  「{c['label']}」 层={c['layer']} 来源={c['basis']}")
    if c.get("basis_uri"):
        lines.append(f"        标准定位 {c['basis_uri']}")
    lines.append("  [3]   祖先链 " + " → ".join(f"{a['layer']}:{a['label']}" for a in chain["ancestry"]))
    if chain["constraints"]:
        for con in chain["constraints"]:
            lines.append(f"  [4]   约束  {con['constraint_id']}: {con['expression']}")
    if chain["derived_uses"]:
        for d in chain["derived_uses"]:
            lines.append(f"  [↩]   被派生使用 {d['concept_id']}「{d['label']}」= {d['formula']}")

    if chain.get("derived"):
        d = chain["derived"]
        if d["value"] is None:
            shown = "（参数不全，算不出）"
        elif d["unit"] == "%":
            shown = f"{d['value']:.2f}%"
        else:
            shown = f"{d['value']:.4f} {d['unit'] or ''}".strip()
        lines.append(f"  [5'] 派生  {d['formula']} = {shown}")
        for a in d["args"]:
            lines.append(f"        ↳ 参数 {a['concept_id'] or a['metric_id']}"
                         f"「{a['label'] or '—'}」 事实 {a['facts']} 条")

    for f in chain["facts"]:
        ev = f["evidence"]
        lines.append(f"  [5]   事实  {f['company']} {f['period']} = {f['value_norm']} {f['unit']}"
                     f"（{f['caliber']}，字典 v{f['dict_version']}）")
        lines.append(f"  [6]   证据  {ev['source_doc']} P{ev['page_num']} / {ev['table_id']}")
        lines.append(f"        原文命中 {ev['raw_text_matched']!r}")
        for p in f["pages"]:
            lines.append(f"  [7]   页面  {p['page']}  {p['anchor'] or '—'}"
                     f"  显示值 {p['display_value']}")
    if chain["breaks"]:
        lines.append("  ⚠ 断点：")
        for b in chain["breaks"]:
            lines.append(f"    - [{b['hop']}] {b['reason']}")
    return "\n".join(lines)


def format_tree(conn: duckdb.DuckDBPyConnection) -> str:
    """打印四层栈的树形视图。"""
    rows = conn.execute(
        "SELECT concept_id, label, layer, parent_id, metric_id FROM concepts ORDER BY layer, concept_id"
    ).fetchall()
    children: dict[str | None, list[tuple]] = {}
    for r in rows:
        children.setdefault(r[3], []).append(r)
    lines: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        for cid, label, layer, _parent, metric_id in children.get(parent, []):
            mark = f" [{metric_id}]" if metric_id else ""
            lines.append(f"{'  ' * depth}{layer} {label}{mark}")
            walk(cid, depth + 1)

    walk(None, 0)
    return "\n".join(lines)
