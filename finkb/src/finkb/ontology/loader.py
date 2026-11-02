"""把 ``config/ontology.yaml`` 灌进 DuckDB（幂等）。

幂等策略用 ``INSERT OR REPLACE``，与 ``FactStore.load_metric_dict`` 一致：
本体是事实表的概念来源，清表重建会让审计看不出「同一概念被换过定义」。
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from finkb.ontology import model
from finkb.fact_engine.store import FACTS_DEDUP_VIEW_SQL
from finkb.ontology.schema import ONTOLOGY_MIGRATION_SQL, ONTOLOGY_SQL


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """建本体表 + 给 facts 补 dict_version（补丁 O3）。"""
    conn.execute(ONTOLOGY_SQL)
    for sql in ONTOLOGY_MIGRATION_SQL:
        conn.execute(sql)
    _ensure_facts_dict_version(conn)


def _ensure_facts_dict_version(conn: duckdb.DuckDBPyConnection) -> None:
    """facts 归 FactStore 所有，这里只做兜底。

    库已建过 facts 才补列并重建 ``facts_dedup`` 视图；空库交给 FactStore 自己建。
    视图必须在 ALTER 之后建：``SELECT *`` 的列在创建时就绑定完了。
    """
    exists = conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'facts'"
    ).fetchone()[0]
    if not exists:
        return
    conn.execute("ALTER TABLE facts ADD COLUMN IF NOT EXISTS dict_version INTEGER")
    conn.execute(FACTS_DEDUP_VIEW_SQL)



def load(
    conn: duckdb.DuckDBPyConnection,
    doc: model.OntologyDoc | None = None,
    path: str | Path | None = None,
) -> dict[str, int]:
    """写入概念/关系/约束，返回各表行数。"""
    doc = doc or model.load(path)
    ensure_schema(conn)

    conn.executemany(
        "INSERT OR REPLACE INTO concepts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                c.concept_id, c.label, c.layer, c.domain, c.basis, c.basis_uri,
                c.basis_element, c.definition, c.parent_id, c.metric_id,
                c.unit_default, c.formula, list(c.expression_args), list(c.aliases),
                c.source_uri, c.valid_from, c.valid_to,
            )
            for c in doc.concepts
        ],
    )

    # 关系与约束是「从属」数据：先按来源清空再灌，避免上次生成但这次删掉的边残留。
    conn.execute("DELETE FROM concept_relations WHERE source_uri IS NOT NULL OR source_uri IS NULL")
    conn.executemany(
        "INSERT INTO concept_relations VALUES (?,?,?,?,?)",
        [(r.src_concept, r.rel_type, r.dst_concept, r.attrs_json, r.source_uri) for r in doc.relations],
    )
    conn.execute("DELETE FROM ontology_constraints")
    conn.executemany(
        "INSERT INTO ontology_constraints "
        "(constraint_id, concept_id, rule_type, expression, tolerance, note, source_uri, severity) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (c.constraint_id, c.concept_id, c.rule_type, c.expression, c.tolerance,
             c.note, c.source_uri, c.severity)
            for c in doc.constraints
        ],
    )

    _sync_dict_version(conn)

    # 维度种类也由本体管辖：POST 一份年报不需要任何维度配置，种类来自这里，
    # 成员来自结构表抽取。只灌种类，成员的 facts 计数由抽取侧维护，不能覆盖。
    conn.executemany(
        "INSERT INTO dimensions (dim_kind, label, markers, note) VALUES (?,?,?,?) "
        "ON CONFLICT (dim_kind) DO UPDATE SET label = EXCLUDED.label, "
        "markers = EXCLUDED.markers, note = EXCLUDED.note",
        [(d.dim_kind, d.label, d.markers, d.note) for d in doc.dimensions],
    )

    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", ["ontology_version", str(doc.version)])
    return {
        "concepts": len(doc.concepts),
        "relations": len(doc.relations),
        "constraints": len(doc.constraints),
        "dimensions": len(doc.dimensions),
    }


def _sync_dict_version(conn: duckdb.DuckDBPyConnection) -> int:
    """把 metric_dict 的版本号回填到 facts.dict_version（O3）。

    只填空值：已标版本的历史事实不覆盖，否则「口径变更前的旧事实」会被
    静默改成新版本号，恰好毁掉这个字段存在的意义。
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'metric_dict_version'"
    ).fetchone()
    if not row:
        return 0
    try:
        version = int(row[0])
    except (TypeError, ValueError):
        return 0
    n = conn.execute(
        "UPDATE facts SET dict_version = ? WHERE dict_version IS NULL", [version]
    ).fetchone()
    return int(n[0]) if n else 0


def counts(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    out = {}
    for table in ("concepts", "concept_relations", "ontology_constraints",
                  "dimensions", "dimension_members", "segment_facts"):
        try:
            out[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        except duckdb.CatalogException:
            out[table] = 0
    return out
