"""闭世界数据校验（P1-2）：约束求值 + 实体回填 + 附注覆盖。

与 ``ontology.audit`` 的分工：audit 查「链条完整性」（登记/祖先/证据/页面），
这里查「数据本身是否自洽」——把 ``ontology_constraints`` 逐条在事实库上求值，
并检查实体主数据是否全部回填、附注锚覆盖是否达标。

只读，不改任何数据。error 级问题会让 ``check.sh`` 变红；warn 只在报告里提示。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from finkb.fact_engine.selector import FINKB_ROOT
from finkb.ontology import expr

DB_PATH = FINKB_ROOT / "data" / "facts.duckdb"

# 附注覆盖只对有附注的文档（年报+半年报，20 份）计算；核心科目要求 ≥80%。
CORE_METRICS = (
    "revenue", "operating_cost", "rd_expense", "selling_expense", "admin_expense",
    "finance_expense", "cash", "inventory", "accounts_receivable",
    "short_term_borrowings", "long_term_borrowings", "bonds_payable", "lease_liabilities",
)
NOTE_COVERAGE_TARGET = 0.8


def _check(check_id: str, severity: str, count: int, message: str, detail: list | None = None) -> dict:
    return {
        "id": check_id,
        "severity": severity,
        "count": count,
        "status": "ok" if count == 0 else "fail",
        "message": message,
        "detail": (detail or [])[:10],
    }


def _pivot(con: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], dict[str, float]]:
    groups: dict[tuple[str, str], dict[str, float]] = {}
    for company, period, metric_id, value in con.execute(
        "SELECT company, period, metric_id, value_norm FROM facts_dedup"
    ).fetchall():
        if value is None:
            continue
        groups.setdefault((company, period), {})[metric_id] = float(value)
    return groups


def _derived(env: dict[str, float], formulas: list[tuple[str, object]]) -> dict[str, float]:
    """按 L3 公式补出派生比率（杜邦等），缺输入就跳过。"""
    out = dict(env)
    changed = True
    while changed:
        changed = False
        for metric_id, node in formulas:
            if metric_id in out:
                continue
            deps = expr.deps(node)
            if deps and deps <= out.keys():
                try:
                    out[metric_id] = expr.evaluate(node, out)
                    changed = True
                except (ZeroDivisionError, KeyError):
                    continue
    return out


def _check_expression(
    constraint_id: str,
    concept_id: str,
    rule_type: str,
    expression: str,
    tolerance: float | None,
    severity: str,
    con: duckdb.DuckDBPyConnection,
    pivot: dict[tuple[str, str], dict[str, float]],
    formulas: list[tuple[str, object]],
) -> dict:
    samples: list[dict] = []
    checked = 0
    skipped_missing = 0

    if rule_type in ("acct_equation", "range"):
        node = expr.parse_condition(expression)
        tol = float(tolerance) if tolerance is not None else 0.05
        deps = expr.condition_deps(node)
        for (company, period), env in pivot.items():
            values = _derived(env, formulas)
            if not deps <= values.keys():
                skipped_missing += 1
                continue
            checked += 1
            if not expr.evaluate_condition(node, values, tol=tol):
                samples.append({
                    "company": company, "period": period,
                    "values": {key: round(values[key], 4) for key in sorted(expr.condition_vars(expression))},
                })
    elif rule_type == "required_period":
        checked = con.execute("SELECT COUNT(*) FROM facts_dedup").fetchone()[0]
        bad = con.execute(
            "SELECT company, period, metric_id FROM facts_dedup "
            "WHERE period IS NULL OR trim(period) = '' LIMIT 10"
        ).fetchall()
        samples.extend({"company": c, "period": p, "metric_id": m} for c, p, m in bad)
    elif rule_type == "unit_check":
        wanted = expression.split("==")[-1].strip().strip("'\"")
        metric_id = con.execute(
            "SELECT metric_id FROM concepts WHERE concept_id = ?", [concept_id]
        ).fetchone()
        metric_id = metric_id[0] if metric_id else None
        checked = con.execute(
            "SELECT COUNT(*) FROM facts_dedup WHERE metric_id = ?", [metric_id]
        ).fetchone()[0] if metric_id else 0
        if metric_id:
            bad = con.execute(
                "SELECT company, period, norm_unit FROM facts_dedup WHERE metric_id = ? AND norm_unit <> ? LIMIT 10",
                [metric_id, wanted],
            ).fetchall()
            samples.extend({"company": c, "period": p, "norm_unit": u, "wanted": wanted} for c, p, u in bad)
    else:
        return _check(constraint_id, severity, 0, f"未识别 rule_type={rule_type}，跳过")

    return _check(
        constraint_id, severity, len(samples),
        f"{rule_type}：{expression}" + (f"（检查 {checked} 组，缺输入跳过 {skipped_missing} 组）" if rule_type in ("acct_equation", "range") else ""),
        samples,
    )


def _audit(con: duckdb.DuckDBPyConnection) -> dict:
    checks: list[dict] = []
    pivot = _pivot(con)
    formulas: list[tuple[str, object]] = []
    for concept_id, metric_id, formula in con.execute(
        "SELECT concept_id, metric_id, formula FROM concepts WHERE layer = 'L3' AND formula IS NOT NULL"
    ).fetchall():
        try:
            formulas.append((metric_id or concept_id.split(":")[-1], expr.parse(formula)))
        except expr.ExprError:
            continue

    constraints = con.execute(
        "SELECT constraint_id, concept_id, rule_type, expression, tolerance, severity FROM ontology_constraints ORDER BY constraint_id"
    ).fetchall()
    for row in constraints:
        checks.append(_check_expression(*row, con, pivot, formulas))

    unresolved = con.execute(
        "SELECT COUNT(*) FROM relations WHERE src_entity_id IS NULL OR dst_entity_id IS NULL"
    ).fetchone()[0]
    checks.append(_check("entity_annotation", "error", unresolved, "关系边有未回填 entity_id 的行"))

    # 附注覆盖：分母只算「有附注锚的文档」（9 份季报没有附注）。
    eligible = con.execute(
        "SELECT COUNT(DISTINCT doc_id) FROM note_anchors WHERE anchor_type = 'note'"
    ).fetchone()[0]
    note_docs = dict(con.execute(
        "SELECT metric_id, COUNT(DISTINCT doc_id) FROM note_anchors WHERE anchor_type = 'note' GROUP BY metric_id"
    ).fetchall())
    low = []
    for metric in CORE_METRICS:
        ratio = (note_docs.get(metric, 0) / eligible) if eligible else 0.0
        if ratio < NOTE_COVERAGE_TARGET:
            low.append({"metric_id": metric, "note_docs": note_docs.get(metric, 0), "eligible": eligible,
                        "ratio": round(ratio, 3)})
    checks.append(_check("note_anchor_coverage", "warn", len(low),
                         f"核心科目附注锚不足 {NOTE_COVERAGE_TARGET:.0%}（分母={eligible} 份有附注文档）", low))

    info = {
        "fact_groups": len(pivot),
        "note_eligible_docs": eligible,
        "statement_anchor_docs": con.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM note_anchors WHERE anchor_type = 'statement'"
        ).fetchone()[0],
        "conflicts": con.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0],
        "relations": con.execute("SELECT COUNT(*) FROM relations").fetchone()[0],
    }
    errors = [item["id"] for item in checks if item["severity"] == "error" and item["status"] == "fail"]
    warnings = [item["id"] for item in checks if item["severity"] == "warn" and item["status"] == "fail"]
    return {
        "counts": {"constraints": len(constraints), "groups": len(pivot)},
        "checks": checks,
        "info": info,
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }


def audit(db_path: Path | str = DB_PATH) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return _audit(con)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="闭世界数据校验（只读）")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit(args.db)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"数据校验：约束 {report['counts']['constraints']} 条，覆盖 {report['counts']['groups']} 个(公司,期间)组")
        for item in report["checks"]:
            mark = "✓" if item["status"] == "ok" else ("✗" if item["severity"] == "error" else "·")
            print(f"  {mark} {item['id']:<26} {item['status']:<4} severity={item['severity']:<5} "
                  f"count={item['count']}  {item['message']}")
        info = report["info"]
        print(f"  实体回填={info['relations']} 条关系 / 附注文档={info['note_eligible_docs']} / "
              f"冲突={info['conflicts']} / 报表页锚文档={info['statement_anchor_docs']}")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
