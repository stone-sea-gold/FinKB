"""本体质量门禁：OntoQA 风格结构指标 + OoPS 式常见建模坑。

只读检查、不改数据。输出分为 error / warn：
- error：结构坏了（层级环、悬空引用、互逆属性不成对），会挡住 ``check.sh``；
- warn：可疑但可接受（孤立概念、重复标签、无 domain/range 的对象属性等）。

设计取舍不照搬 COA 的三层校验（HermiT/OntoQA/OoPS!）：本机无 Java，
HermiT 跑不了；这里覆盖的是同样能在 DuckDB 里确定性检查的那部分。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from finkb.fact_engine.selector import FINKB_ROOT

DB_PATH = FINKB_ROOT / "data" / "facts.duckdb"


def _norm(text: str) -> str:
    return "".join(str(text).split()).lower()


def _check(check_id: str, severity: str, count: int, message: str, detail: list | None = None) -> dict:
    return {
        "id": check_id,
        "severity": severity,
        "count": count,
        "status": "ok" if count == 0 else "fail",
        "message": message,
        "detail": (detail or [])[:10],
    }


def _audit(con: duckdb.DuckDBPyConnection) -> dict:
    counts = {
        "concepts": con.execute("SELECT COUNT(*) FROM concepts").fetchone()[0],
        "concept_relations": con.execute("SELECT COUNT(*) FROM concept_relations").fetchone()[0],
        "ontology_classes": con.execute("SELECT COUNT(*) FROM ontology_classes").fetchone()[0],
        "object_properties": con.execute("SELECT COUNT(*) FROM ontology_object_properties").fetchone()[0],
        "defined_classes": con.execute("SELECT COUNT(*) FROM ontology_defined_classes").fetchone()[0],
        "inferred_concepts": con.execute("SELECT COUNT(*) FROM inferred_concepts").fetchone()[0],
    }
    checks: list[dict] = []

    # 1. 重复标签（忽略空白与大小写）
    labels: dict[str, list[str]] = {}
    for concept_id, label in con.execute("SELECT concept_id, label FROM concepts").fetchall():
        labels.setdefault(_norm(label), []).append(concept_id)
    duplicates = {key: ids for key, ids in labels.items() if len(ids) > 1}
    checks.append(_check(
        "duplicate_labels", "warn", len(duplicates),
        "概念标签重复（同一标签指向多个概念）",
        [{"label": key, "concepts": ids} for key, ids in duplicates.items()],
    ))

    # 2. 孤立概念：既不是关系起点也不是终点；L0 元概念与维度概念允许孤立
    concept_ids = {row[0] for row in con.execute("SELECT concept_id FROM concepts").fetchall()}
    linked = set()
    for src, dst in con.execute("SELECT src_concept, dst_concept FROM concept_relations").fetchall():
        linked.update((src, dst))
    isolated_rows = con.execute(
        "SELECT concept_id FROM concepts WHERE layer NOT LIKE 'L0%' AND domain <> 'dimension'"
    ).fetchall()
    orphans = sorted(row[0] for row in isolated_rows if row[0] not in linked)
    checks.append(_check("orphan_concepts", "warn", len(orphans), "未参与任何关系的概念", orphans))

    # 3. 层级环（parent_id 与 is_a 边合并检查）
    parent = {cid: pid for cid, pid in con.execute(
        "SELECT concept_id, parent_id FROM concepts WHERE parent_id IS NOT NULL"
    ).fetchall()}
    for src, dst in con.execute(
        "SELECT src_concept, dst_concept FROM concept_relations WHERE rel_type = 'is_a'"
    ).fetchall():
        parent.setdefault(src, dst)
    cycle_nodes: set[str] = set()
    for start in parent:
        seen: list[str] = []
        node = start
        while node in parent and node not in seen:
            seen.append(node)
            node = parent[node]
        if node == start or node in seen:
            cycle_nodes.update(seen[seen.index(node):] if node in seen else seen)
    checks.append(_check("hierarchy_cycles", "error", len(cycle_nodes), "概念层级存在环", sorted(cycle_nodes)))

    # 4. 悬空引用：关系两端、父概念、对象属性 domain/range 必须能在概念/类里找到
    known = concept_ids | {row[0] for row in con.execute("SELECT class_id FROM ontology_classes").fetchall()}
    dangling: list[dict] = []
    for src, dst in con.execute("SELECT src_concept, dst_concept FROM concept_relations").fetchall():
        if src not in concept_ids or dst not in concept_ids:
            dangling.append({"src": src, "dst": dst})
    missing_parent = sorted({pid for pid in parent.values() if pid not in known})
    checks.append(_check(
        "dangling_references", "error", len(dangling) + len(missing_parent),
        "关系或父概念指向不存在的 id",
        dangling[:5] + [{"missing_parent": pid} for pid in missing_parent[:5]],
    ))

    # 5. 对象属性缺 domain/range（会削弱 OWL 导出后的推理）
    rows = con.execute(
        "SELECT property_id FROM ontology_object_properties "
        "WHERE (domain_class IS NULL OR domain_class = '') OR (range_class IS NULL OR range_class = '')"
    ).fetchall()
    checks.append(_check("missing_domain_range", "warn", len(rows), "对象属性缺 domain 或 range",
                         [row[0] for row in rows]))

    # 6. 互逆属性必须成对
    inverse = dict(con.execute(
        "SELECT property_id, inverse_of FROM ontology_object_properties WHERE inverse_of IS NOT NULL"
    ).fetchall())
    mismatched = []
    for prop, partner in inverse.items():
        if inverse.get(partner) != prop:
            mismatched.append({"property": prop, "inverse_of": partner,
                               "partner_inverse_of": inverse.get(partner)})
    checks.append(_check("inverse_mismatch", "error", len(mismatched), "互逆属性未成对声明", mismatched))

    # 7. L2 科目概念是否都有事实（Tier 2 只该跑有数据的指标）
    metric_concepts = con.execute(
        "SELECT concept_id, metric_id FROM concepts WHERE layer = 'L2' AND metric_id IS NOT NULL"
    ).fetchall()
    fact_counts = dict(con.execute(
        "SELECT metric_id, COUNT(*) FROM facts_dedup GROUP BY metric_id"
    ).fetchall())
    unmapped = [{"concept_id": cid, "metric_id": mid} for cid, mid in metric_concepts if fact_counts.get(mid, 0) == 0]
    checks.append(_check("unmapped_metrics", "warn", len(unmapped), "L2 科目概念在事实库里没有数据", unmapped))

    # 8. 定义类是否真的推出过命中（只提示，不否决）
    hit_counts = dict(con.execute(
        "SELECT class_id, COUNT(*) FROM inferred_concepts GROUP BY class_id"
    ).fetchall())
    defined = [row[0] for row in con.execute("SELECT class_id FROM ontology_defined_classes").fetchall()]
    no_hits = [cid for cid in defined if hit_counts.get(cid, 0) == 0]
    checks.append(_check("defined_class_no_hits", "warn", len(no_hits), "定义类从未推出任何实例", no_hits))

    # 9. 定义覆盖率（概念无 definition 文本）
    no_definition = [row[0] for row in con.execute(
        "SELECT concept_id FROM concepts WHERE definition IS NULL OR trim(definition) = ''"
    ).fetchall()]
    checks.append(_check("missing_definition", "warn", len(no_definition), "概念缺 definition 文本", no_definition))

    errors = [item["id"] for item in checks if item["severity"] == "error" and item["status"] == "fail"]
    warnings = [item["id"] for item in checks if item["severity"] == "warn" and item["status"] == "fail"]
    db_row = con.execute("PRAGMA database_list").fetchone()
    return {
        "db": str((db_row[-1] if db_row else None) or ":memory:"),
        "counts": counts,
        "checks": checks,
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
    parser = argparse.ArgumentParser(description="本体质量门禁（只读）")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit(args.db)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"本体质量检查：concepts={report['counts']['concepts']} relations={report['counts']['concept_relations']} "
              f"classes={report['counts']['ontology_classes']} properties={report['counts']['object_properties']} "
              f"defined={report['counts']['defined_classes']}")
        for item in report["checks"]:
            mark = "✓" if item["status"] == "ok" else ("✗" if item["severity"] == "error" else "·")
            print(f"  {mark} {item['id']:<22} {item['status']:<4} severity={item['severity']:<5} "
                  f"count={item['count']}  {item['message']}")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
