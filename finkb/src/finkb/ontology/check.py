"""约束校验：拿真实事实去验本体里的会计等式与取值范围。

**只校验不推导**（方案 §7.5.6）：这里发现"不等"就报出来，绝不自动修正事实。
校验失败也不阻断写库——它是一道体检，不是写入门禁。

severity 两档（构建时被真实数据逼出来的设计）：

* ``error`` —— 会计恒等式。违反 = 数据错误，``check`` 命令返回非零。
* ``warn``  —— 通常成立但**允许合法例外**。违反只提示人工看一眼，不阻断。

最初把 ``net_profit <= total_profit`` 写成 error，实测 17 个 (公司,期间) 违反，
逐个核对后发现约束本身错了：所得税费用为负（亏损年度确认递延所得税资产）或
少数股东损益为负时，归母净利润**可以大于**利润总额。长安 000625 2024 就是
（利润总额 67.6 亿 < 归母净利 73.2 亿，同时有 -10 亿少数股东损益）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import duckdb

from finkb.ontology import derive, expr

# 会计等式的相对容差。事实以「亿元」存储且保留 2 位小数，三项相加的四舍五入
# 误差可达 0.03 亿，所以给一个小的绝对下限兜底。
DEFAULT_TOL_PCT = 0.01
MIN_ABS_TOL = 0.05

# unit_check 允许检查的列。为什么不止 unit_of_account：后者是原始表格的表头
# 单位标注（每股指标常写成「元」），真正该验的是规范化之后的 norm_unit。
UNIT_COLUMNS = ("unit_of_account", "norm_unit", "unit_family")


@dataclass
class Violation:
    constraint_id: str
    company: str
    period: str
    expression: str
    detail: str
    severity: str = "error"
    values: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _subtree_metrics(conn: duckdb.DuckDBPyConnection, concept_id: str) -> list[str]:
    rows = conn.execute(
        """
        WITH RECURSIVE sub(concept_id) AS (
            SELECT concept_id FROM concepts WHERE concept_id = ?
            UNION ALL
            SELECT c.concept_id FROM concepts c JOIN sub ON c.parent_id = sub.concept_id
        )
        SELECT metric_id FROM concepts
        WHERE concept_id IN (SELECT concept_id FROM sub) AND metric_id IS NOT NULL
        """,
        [concept_id],
    ).fetchall()
    return [r[0] for r in rows]


def _targets(conn: duckdb.DuckDBPyConnection, metrics: list[str]) -> list[tuple[str, str]]:
    if not metrics:
        return []
    placeholders = ",".join("?" * len(metrics))
    return conn.execute(
        f"SELECT DISTINCT company, period FROM facts WHERE metric_id IN ({placeholders}) "
        "AND value_norm IS NOT NULL ORDER BY company, period",
        metrics,
    ).fetchall()


def _parse_unit_check(expression: str) -> tuple[str, str]:
    """``norm_unit == '元/股'`` → ``("norm_unit", "元/股")``。"""
    left, _, right = expression.partition("==")
    col = left.strip()
    if col not in UNIT_COLUMNS:
        raise ValueError(f"unit_check 只能查 {UNIT_COLUMNS}，收到 {col!r}")
    return col, right.strip().strip("'\"")


def run(conn: duckdb.DuckDBPyConnection, tol_pct: float = DEFAULT_TOL_PCT) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT constraint_id, concept_id, rule_type, expression, tolerance, severity "
        "FROM ontology_constraints ORDER BY constraint_id"
    ).fetchall()
    violations: list[Violation] = []
    checks_run = 0
    unsupported: list[dict[str, str]] = []

    cache: dict[tuple[str, str], dict[str, float]] = {}

    def env_for(company: str, period: str) -> dict[str, float]:
        key = (company, period)
        if key not in cache:
            cache[key], _ = derive.compute(conn, company, period)
        return cache[key]

    for cid, concept_id, rule_type, expression, tol_override, severity in rows:
        checks_run += 1
        severity = severity or "error"
        metrics = _subtree_metrics(conn, concept_id)
        placeholders = ",".join("?" * len(metrics))

        if rule_type == "required_period":
            if not metrics:
                continue
            n = conn.execute(
                f"SELECT count(*) FROM facts WHERE metric_id IN ({placeholders}) "
                "AND (period IS NULL OR period = '')",
                metrics,
            ).fetchone()[0]
            if n:
                violations.append(Violation(cid, "*", "*", expression,
                                            f"{n} 条期间类事实缺 period", severity, {}))
            continue

        if rule_type == "unit_check":
            try:
                col, want = _parse_unit_check(expression)
            except ValueError as e:
                unsupported.append({"constraint_id": cid, "rule_type": rule_type,
                                    "reason": str(e)})
                continue
            if not metrics:
                continue
            bad = conn.execute(
                f"SELECT company, period, {col} FROM facts "
                f"WHERE metric_id IN ({placeholders}) AND {col} <> ? LIMIT 5",
                [*metrics, want],
            ).fetchall()
            for company, period, unit in bad:
                violations.append(Violation(
                    cid, company, period, expression,
                    f"{col} 应为 {want}，实际 {unit}", severity, {}))
            continue

        if rule_type not in ("acct_equation", "range"):
            unsupported.append({"constraint_id": cid, "rule_type": rule_type})
            continue

        try:
            cmps = expr.parse_comparison(expression)
        except expr.ExprError as e:
            unsupported.append({"constraint_id": cid, "rule_type": rule_type,
                                "reason": f"表达式无法解析：{e}"})
            continue

        vars_: set[str] = set()
        for c in cmps:
            vars_ |= expr.deps(c.left) | expr.deps(c.right)
        # 只拿涉及的指标，避免为一条约束扫全库
        metric_vars = [v for v in vars_ if v in set(metrics) or _is_metric(conn, v)]
        targets = _targets(conn, metric_vars) if metric_vars else []
        for company, period in targets:
            env = env_for(company, period)
            if any(v not in env for v in vars_):
                continue
            values = {v: env[v] for v in sorted(vars_)}
            for c in cmps:
                rhs = expr.evaluate(c.right, env)
                tol = (tol_override if tol_override is not None else tol_pct) * abs(rhs)
                tol = max(tol, MIN_ABS_TOL if c.op in ("==", "!=") else 0.0)
                if not expr.eval_comparison(c, env, tol=tol):
                    violations.append(Violation(
                        cid, company, period, expression,
                        f"左值 {expr.evaluate(c.left, env):.4f} {c.op} "
                        f"右值 {rhs:.4f} 不成立（容差 {tol:.4f}）",
                        severity, values))

    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity != "error"]
    return {
        "constraints_checked": checks_run,
        "violations": [v.to_dict() for v in violations],
        "errors": len(errors),
        "warnings": len(warnings),
        "unsupported": unsupported,
        "pairs_evaluated": len(cache),
    }


_metric_cache: dict[str, set[str]] = {}


def _is_metric(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    if "set" not in _metric_cache:
        _metric_cache["set"] = {r[0] for r in
                                conn.execute("SELECT metric_id FROM metric_dict").fetchall()}
    return name in _metric_cache["set"]
