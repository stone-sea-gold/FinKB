"""派生指标计算 + 单位校验（L3）。

L3 的公式写在 ontology.yaml 里，参数是 L2 的 metric_id。这里只做「取值 + 套公式」，
**不做四舍五入以外的任何加工**（保留 float，显示层再格式化）。
"""

from __future__ import annotations

import duckdb

from finkb.ontology import expr


class DeriveError(ValueError):
    pass


def l3_concepts(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        "SELECT concept_id, label, formula, expression_args, unit_default FROM concepts "
        "WHERE layer = 'L3' AND formula IS NOT NULL ORDER BY concept_id"
    ).fetchall()
    return [
        {"concept_id": r[0], "label": r[1], "formula": r[2], "args": list(r[3] or []),
         "unit": r[4]}
        for r in rows
    ]


def fact_values(
    conn: duckdb.DuckDBPyConnection, company: str, period: str
) -> dict[str, float]:
    rows = conn.execute(
        "SELECT metric_id, value_norm FROM facts_dedup "
        "WHERE company = ? AND period = ? AND value_norm IS NOT NULL",
        [company, period],
    ).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def compute(
    conn: duckdb.DuckDBPyConnection, company: str, period: str
) -> tuple[dict[str, float], list[str]]:
    """返回 (值字典, 计算失败说明)。L3 之间允许互相引用，按公式序递归求值。"""
    env = fact_values(conn, company, period)
    errors: list[str] = []
    pending = l3_concepts(conn)
    # 简单不动点：最多迭代轮数 = 指标数
    for _ in range(len(pending) + 1):
        progressed = False
        for c in pending:
            key = c["concept_id"]
            if key in env:
                continue
            try:
                node = expr.parse(c["formula"])
                raw = expr.evaluate(node, env)
                # 百分比口径统一。L2 的披露值本身就是「百分数」（15.87 = 15.87%），
                # 而公式算出来是小数（0.1774）。整个知识库只能有一种口径，否则
                # 把 L2 的 ROE 和 L3 的毛利率放一起比会差 100 倍。缩放放在这里，
                # 因为这是唯一一处「由本项目定义而非抄自披露值」的数值。
                env[key] = raw * 100.0 if c["unit"] == "%" else raw
                progressed = True
            except expr.ExprError:
                continue
        if not progressed:
            break
    for c in pending:
        if c["concept_id"] not in env:
            missing = [a for a in c["args"] if a not in env]
            errors.append(
                f"{c['concept_id']}: 缺参数 {missing}" if missing
                else f"{c['concept_id']}: 公式无法求值 {c['formula']!r}"
            )
    return env, errors


def companies_periods(conn: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT DISTINCT company, period FROM facts WHERE value_norm IS NOT NULL "
        "ORDER BY company, period"
    ).fetchall()
