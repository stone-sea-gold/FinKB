"""指标治理 v1：Tier 1 指标清单的结构/SQL/数据可用性检查（ADR-060）。

这里不执行用户问题，只回答三件事：

1. ``config/metrics.yaml`` 声明的指标是否完整、引用是否存在；
2. 这些指标的 SQL 是否只读、参数个数是否对得上、在真实库上能否执行；
3. 指标关联的本体概念是否存在，未治理的派生指标有没有被记下来。

硬错误（结构坏、SQL 危险、引用缺失、执行报错）退出 1；软问题（缺别名、
样本期没有数据、pending 指标）只警告。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml

from finkb.fact_engine.metric_dict import load_metric_dict
from finkb.fact_engine.selector import FINKB_ROOT

DEFAULT_METRICS = FINKB_ROOT / "config" / "metrics.yaml"
DEFAULT_QUERIES = FINKB_ROOT / "config" / "wiki_queries.yaml"
DEFAULT_DICT = FINKB_ROOT / "config" / "metric_dict.yaml"
DEFAULT_ONTOLOGY = FINKB_ROOT / "config" / "ontology.yaml"
DEFAULT_DB = FINKB_ROOT / "data" / "facts.duckdb"

# ADR-001：数字只由 SQL 计算；这里的 SQL 必须是只读查询。
FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|COPY|PRAGMA|CALL|"
    r"EXPORT|IMPORT|LOAD|INSTALL|VACUUM|REPLACE|TRUNCATE)\b",
    re.IGNORECASE,
)
PARAM_NAMES = {"company", "period", "metric_id", "fact_id", "fiscal_year"}


@dataclass
class GovernanceResult:
    ok: bool
    governed: dict[str, int]
    hard_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sql_checked: int = 0
    db_checked: bool = False

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "governed": self.governed,
            "hard_errors": self.hard_errors,
            "warnings": self.warnings,
            "sql_checked": self.sql_checked,
            "db_checked": self.db_checked,
        }


def count_params(sql: str) -> int:
    """参数占位符个数；本项目 SQL 只在 WHERE 里用 ``?``，不做复杂词法。"""
    return sql.count("?")


def check_sql_safety(sql: str) -> list[str]:
    """只读与单语句检查；返回错误列表（空表示通过）。"""
    errors: list[str] = []
    body = (sql or "").strip().rstrip(";").strip()
    if not body:
        return ["SQL 为空"]
    if ";" in body:
        errors.append("SQL 含多条语句（只允许一条 SELECT）")
    if not re.match(r"^(SELECT|WITH)\b", body, re.IGNORECASE):
        errors.append("SQL 必须以 SELECT 或 WITH 开头")
    if FORBIDDEN_RE.search(body):
        errors.append("SQL 含禁用的写/危险关键字")
    return errors


def _load_yaml(path: str | Path) -> dict:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: 顶层必须是映射")
    return raw


def _ontology_concepts(path: str | Path) -> set[str]:
    raw = _load_yaml(path)
    return {str(item["concept_id"]) for item in raw.get("concepts", []) if item.get("concept_id")}


def _sample_row(con: duckdb.DuckDBPyConnection) -> tuple[str, str, int] | None:
    # 选事实最多的 (公司, 期间) 做样本，避免用冷门期间误报“没有数据”。
    row = con.execute(
        "SELECT company, period, MIN(fact_id) FROM facts_dedup "
        "GROUP BY 1, 2 ORDER BY COUNT(*) DESC, company, period LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return str(row[0]), str(row[1]), int(row[2])


def _params_for(names: list[str], sample: tuple[str, str, int], *, metric_id: str | None = None) -> list:
    company, period, fact_id = sample
    values: list = []
    for name in names:
        if name == "company":
            values.append(company)
        elif name == "period":
            values.append(period)
        elif name == "fact_id":
            values.append(fact_id)
        elif name == "metric_id" and metric_id is not None:
            values.append(metric_id)
        else:
            raise ValueError(f"未知参数名 {name!r}（允许 {sorted(PARAM_NAMES)}）")
    return values


def audit(
    *,
    db_path: str | Path | None = DEFAULT_DB,
    metrics_path: str | Path = DEFAULT_METRICS,
    queries_path: str | Path = DEFAULT_QUERIES,
    dict_path: str | Path = DEFAULT_DICT,
    ontology_path: str | Path = DEFAULT_ONTOLOGY,
    check_db: bool = True,
) -> GovernanceResult:
    """执行一次指标治理检查；不抛异常，问题都收进 hard_errors/warnings。"""
    hard: list[str] = []
    warn: list[str] = []
    result = GovernanceResult(ok=True, governed={}, hard_errors=hard, warnings=warn)

    try:
        config = _load_yaml(metrics_path)
        queries = _load_yaml(queries_path).get("queries") or {}
        metrics = load_metric_dict(dict_path)
    except (OSError, ValueError, KeyError) as exc:
        hard.append(f"配置加载失败：{exc}")
        result.ok = False
        return result

    raw_ids = [m.metric_id for m in metrics]
    overrides = config.get("metrics") or {}
    pending = config.get("pending") or {}
    if not isinstance(overrides, dict) or not isinstance(pending, dict):
        hard.append("metrics/pending 必须是映射")
        result.ok = False
        return result

    # 1) 名称/单位/别名/kind
    for metric_id, spec in overrides.items():
        if not isinstance(spec, dict):
            hard.append(f"{metric_id}: 定义必须是映射")
            continue
        if not spec.get("name"):
            hard.append(f"{metric_id}: 缺少 name")
        if not spec.get("unit"):
            hard.append(f"{metric_id}: 缺少 unit")
        if not spec.get("aliases"):
            warn.append(f"{metric_id}: 没有 aliases（Tier 1 路由会变弱）")
        kind = spec.get("kind", "query")
        if kind not in {"query", "fact"}:
            hard.append(f"{metric_id}: kind={kind!r} 只能是 query/fact")
        if kind == "query" and not spec.get("ref"):
            hard.append(f"{metric_id}: kind=query 但缺少 ref")

    # 2) 引用存在、SQL 只读、参数对得上
    sql_to_check: dict[str, tuple[str, list[str], str]] = {}
    for metric_id, spec in overrides.items():
        if not isinstance(spec, dict) or spec.get("kind", "query") != "query":
            continue
        ref = spec.get("ref")
        if ref not in queries:
            hard.append(f"{metric_id}: 引用的命名 SQL {ref!r} 不在 wiki_queries.yaml")
            continue
        entry = queries[ref]
        sql = str(entry.get("sql") or "")
        params = list(entry.get("params") or [])
        errors = check_sql_safety(sql)
        for error in errors:
            hard.append(f"{metric_id}（{ref}）: {error}")
        bad_params = [name for name in params if name not in PARAM_NAMES]
        if bad_params:
            hard.append(f"{metric_id}（{ref}）: 未知参数名 {sorted(set(bad_params))}")
        if count_params(sql) != len(params):
            hard.append(f"{metric_id}（{ref}）: SQL 参数个数 {count_params(sql)} 与 params {len(params)} 不一致")
        if ref in sql_to_check:
            continue
        sql_to_check[ref] = (sql, params, metric_id)
        if entry.get("unit") and spec.get("unit") and entry["unit"] != spec["unit"]:
            warn.append(f"{metric_id}: metrics.unit={spec['unit']!r} 与 wiki_queries.unit={entry['unit']!r} 不一致")

    fact_sql = str(config.get("fact_sql") or "")
    if fact_sql:
        for error in check_sql_safety(fact_sql):
            hard.append(f"fact_sql: {error}")
        if count_params(fact_sql) != 3:
            hard.append(f"fact_sql: 参数个数应为 3（company/period/metric_id），实际 {count_params(fact_sql)}")
    else:
        warn.append("没有声明 fact_sql，直接披露科目的查询模板不可检查")

    # 3) 本体概念存在
    try:
        concepts = _ontology_concepts(ontology_path)
    except (OSError, ValueError) as exc:
        warn.append(f"本体文件读取失败，跳过概念检查：{exc}")
        concepts = set()
    for metric_id, spec in overrides.items():
        if not isinstance(spec, dict):
            continue
        for concept in spec.get("ontology_concepts") or []:
            if concepts and concept not in concepts:
                hard.append(f"{metric_id}: ontology_concepts 里的 {concept!r} 不在 ontology.yaml")
    for metric_id, spec in pending.items():
        if not isinstance(spec, dict) or not spec.get("name"):
            hard.append(f"pending.{metric_id}: 缺少 name")
        warn.append(f"未治理指标 pending.{metric_id}: {spec.get('name', '')}（{spec.get('reason', '无原因')}）")
        for concept in (spec or {}).get("ontology_concepts") or []:
            if concepts and concept not in concepts:
                hard.append(f"pending.{metric_id}: ontology_concepts 里的 {concept!r} 不在 ontology.yaml")

    # 4) 在真实库上执行（只读）
    effective_raw = [metric_id for metric_id in raw_ids if metric_id not in overrides]
    result.governed = {
        "raw": len(effective_raw),
        "query": len(sql_to_check),
        "pending": len(pending),
        "total": len(effective_raw) + len(sql_to_check),
    }
    db = Path(db_path) if db_path else None
    if check_db and db and db.exists():
        result.db_checked = True
        try:
            con = duckdb.connect(str(db), read_only=True)
            try:
                sample = _sample_row(con)
                if sample is None:
                    warn.append("facts_dedup 为空，跳过 SQL 执行检查")
                else:
                    for ref, (sql, params, metric_id) in sql_to_check.items():
                        try:
                            values = _params_for(params, sample)
                            rows = con.execute(sql, values).fetchall()
                        except Exception as exc:  # noqa: BLE001 - 检查脚本要收集所有错误
                            hard.append(f"{metric_id}（{ref}）执行失败：{exc}")
                            continue
                        result.sql_checked += 1
                        if not rows:
                            warn.append(f"{metric_id}（{ref}）在样本 {sample[0]}/{sample[1]} 上没有数据")
                    if fact_sql and effective_raw:
                        try:
                            values = _params_for(
                                ["company", "period", "metric_id"], sample, metric_id=effective_raw[0]
                            )
                            rows = con.execute(fact_sql, values).fetchall()
                            result.sql_checked += 1
                            if not rows:
                                warn.append(f"fact_sql 在样本 {sample[0]}/{sample[1]}/{effective_raw[0]} 上没有数据")
                        except Exception as exc:  # noqa: BLE001
                            hard.append(f"fact_sql 执行失败：{exc}")
                    # 覆盖率：聚合一次，避免每个指标都报
                    counts = dict(con.execute(
                        "SELECT metric_id, COUNT(*) FROM facts_dedup GROUP BY metric_id"
                    ).fetchall())
                    missing = [metric_id for metric_id in effective_raw if not counts.get(metric_id)]
                    if missing:
                        warn.append(f"{len(missing)} 个直接披露科目在 facts_dedup 里没有数据：{sorted(missing)}")
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            hard.append(f"打开事实库失败：{exc}")
    elif check_db:
        warn.append(f"事实库不存在，跳过 SQL 执行检查：{db}")

    result.ok = not hard
    return result
