"""本体层命令行。全部输出都支持 ``--format json``，便于机器读取。

    python -m finkb.ontology load                      # YAML → DuckDB（幂等）
    python -m finkb.ontology tree                      # 四层栈树形图
    python -m finkb.ontology chain --metric revenue    # 六跳链条
    python -m finkb.ontology chain --metric revenue --company 002594 --format json
    python -m finkb.ontology check                     # 跑约束校验
    python -m finkb.ontology derive --company 002594 --period 2025
    python -m finkb.ontology concept --query 毛利       # 按中文名/别名找概念
    python -m finkb.ontology axioms                    # 公理层：类/对象属性/属性链/数值规则
    python -m finkb.ontology export --out data/ontology  # YAML → TBox/ABox Turtle
    python -m finkb.ontology infer --out data/ontology   # 定义类 + 属性链 + OWL 2 RL 闭包
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

from finkb.ontology import chain, check, derive, loader, model


def _connect(db: str | Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db))


def _attach_wiki(conn: duckdb.DuckDBPyConnection, wiki: str | Path | None) -> None:
    if not wiki:
        return
    p = Path(wiki)
    if not p.exists():
        return
    try:
        conn.execute(f"ATTACH '{p}' AS wiki (READ_ONLY)")
    except duckdb.Error:
        pass  # 已 ATTACH 或库被占用，跳页面跳即可


def _emit(obj: dict | list, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    else:
        print(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_load(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    try:
        counts = loader.load(conn, path=args.ontology)
        print(json.dumps(counts, ensure_ascii=False))
        print(f"库内：{loader.counts(conn)}")
        return 0
    finally:
        conn.close()


def cmd_tree(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    try:
        print(chain.format_tree(conn))
        return 0
    finally:
        conn.close()


def cmd_audit(args: argparse.Namespace) -> int:
    from finkb.ontology import audit

    conn = _connect(args.db)
    try:
        _attach_wiki(conn, args.wiki)
        report = audit.audit(conn, claims_conn=conn)
    finally:
        conn.close()

    if args.format == "json":
        _emit(report, "json")
        return 0

    names = {"concept": "①概念登记", "ancestry": "②祖先链", "constraints": "③约束覆盖",
             "evidence": "④证据字段", "pages": "⑤页面引用", "derived": "⑥派生可算",
             "inference": "⑦推理血缘"}
    print(f"事实总数 {report['total_facts']}；概念 "
          + " ".join(f"{k}={v}" for k, v in report["concepts"]["by_layer"].items()))
    for hop in audit.HOPS:
        n = report["hops"][hop]
        if n is None:
            reason = "尚未运行 ontology infer，跳过" if hop == "inference" else "未接 claims 库，跳过"
            print(f"  {names[hop]:<8} —（{reason}）")
            continue
        mark = "✓" if n == 0 else ("·" if hop in ("pages", "derived") else "✗")
        print(f"  {names[hop]:<8} {mark} {n}"
              + ("" if n == 0 else f"  {report['samples'][hop][:3]}"))
    if report["concepts"]["l2_without_facts"]:
        print("  L2 无事实：" + ", ".join(report["concepts"]["l2_without_facts"]))
    # ⑤⑥ 是覆盖率指标（页面只引用结论、派生受抽取覆盖限制），不参与门禁；
    # ①②③④ 是结构断点，任何一条非零都说明「这个数凭什么信」答不上来。
    broken = [h for h in ("concept", "ancestry", "constraints", "evidence", "inference")
              if report["hops"][h]]
    if args.strict and broken:
        print("  ✗ 结构性断点：" + ", ".join(f"{h}={report['hops'][h]}" for h in broken))
        return 1
    return 0


def cmd_chain(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    try:
        _attach_wiki(conn, args.wiki)
        result = chain.resolve(
            conn, args.metric, company=args.company, period=args.period,
            with_pages=not args.no_pages,
        )
        if args.format == "json":
            _emit(result, "json")
        else:
            print(chain.format_text(result))
        return 0 if result["concept"] and result["facts"] else 1
    finally:
        conn.close()


def cmd_check(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    try:
        report = check.run(conn, tol_pct=args.tolerance)
        if args.format == "json":
            _emit(report, "json")
        else:
            print(f"约束 {report['constraints_checked']} 条，"
                  f"求值 (公司,期间) {report['pairs_evaluated']} 组："
                  f"错误 {report['errors']} 条 / 警告 {report['warnings']} 条")
            for v in report["violations"]:
                mark = "✗" if v["severity"] == "error" else "!"
                print(f"  {mark} [{v['severity']}] {v['constraint_id']} "
                      f"{v['company']} {v['period']}: {v['detail']}")
            for u in report["unsupported"]:
                print(f"  ? {u['constraint_id']}: {u.get('reason') or u['rule_type']} 未实现")
        return 0 if not report["errors"] else 2
    finally:
        conn.close()


def cmd_derive(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    try:
        env, errors = derive.compute(conn, args.company, args.period)
        if args.format == "json":
            _emit({"company": args.company, "period": args.period,
                   "values": env, "errors": errors}, "json")
        else:
            print(f"{args.company} {args.period}：{len(env)} 个指标可算")
            for c in derive.l3_concepts(conn):
                v = env.get(c["concept_id"])
                shown = f"{v:.4f} {c['unit'] or ''}" if v is not None else "—"
                print(f"  {c['label']:8s} {shown}")
            for e in errors:
                print(f"  ! {e}")
        return 0
    finally:
        conn.close()


def cmd_concept(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    try:
        if args.id:
            rows = conn.execute("SELECT * FROM concepts WHERE concept_id = ?", [args.id]).fetchall()
        else:
            kw = f"%{args.query}%"
            rows = conn.execute(
                "SELECT * FROM concepts WHERE label LIKE ? OR metric_id LIKE ? "
                "OR EXISTS (SELECT 1 FROM unnest(aliases) AS t(alias) WHERE alias LIKE ?) "
                "ORDER BY layer, concept_id LIMIT 20",
                [kw, kw, kw],
            ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM concepts LIMIT 0").description]
        out = [dict(zip(cols, r)) for r in rows]
        if args.format == "json":
            _emit(out, "json")
        else:
            for c in out:
                print(f"[{c['layer']}] {c['label']}  ({c['concept_id']})  "
                      f"metric={c['metric_id']}  basis={c['basis']}")
                if c.get("basis_uri"):
                    print(f"        {c['basis_uri']}")
                if c.get("formula"):
                    print(f"        公式 {c['formula']}")
        return 0
    finally:
        conn.close()


def cmd_dims(args: argparse.Namespace) -> int:
    """列出维度种类与成员分布。

    维度种类来自本体（closed vocabulary），成员来自结构表抽取（open set）。
    这里把两者并排打出来，就是为了让「新公司要不要加配置」这个问题一眼有答案：
    不需要，成员自己长出来。
    """
    conn = _connect(args.db)
    try:
        kinds = conn.execute(
            "SELECT dim_kind, label, markers FROM dimensions ORDER BY dim_kind"
        ).fetchall()
        out = []
        for kind, label, markers in kinds:
            members = conn.execute(
                "SELECT member_id, label, facts, aliases FROM dimension_members "
                "WHERE dim_kind = ? ORDER BY facts DESC, member_id",
                [kind],
            ).fetchall()
            facts = conn.execute(
                "SELECT count(*) FROM segment_facts WHERE dimension = ?", [kind]
            ).fetchone()[0]
            out.append({
                "dim_kind": kind,
                "label": label,
                "markers": markers,
                "segment_facts": facts,
                "members": [
                    {"member_id": m[0], "label": m[1], "facts": m[2],
                     "aliases": [a for a in str(m[3] or "").split("|") if a]}
                    for m in members
                ],
            })
        if args.format == "json":
            _emit(out, "json")
        else:
            for d in out:
                print(f"{d['dim_kind']:<10} {d['label']}  标记={d['markers']!r}  "
                      f"成员={len(d['members'])}  结构事实={d['segment_facts']}")
                for m in d["members"][:8]:
                    print(f"    {m['member_id']:<28} facts={m['facts']:<3} "
                          f"aliases={m['aliases']}")
                if len(d["members"]) > 8:
                    print(f"    …… 另有 {len(d['members']) - 8} 个成员")
        return 0
    finally:
        conn.close()


def cmd_axioms(args: argparse.Namespace) -> int:
    doc = model.load()
    ax = model.load_axioms(doc=doc)
    payload = {
        "version": ax.version,
        "namespace": ax.namespace,
        "classes": [
            {"class_id": c.class_id, "label": c.label, "parent": c.parent, "comment": c.comment}
            for c in ax.classes
        ],
        "object_properties": [
            {"property_id": p.property_id, "label": p.label, "domain": p.domain,
             "range": p.range, "inverse_of": p.inverse_of,
             "characteristics": p.characteristics, "comment": p.comment}
            for p in ax.object_properties
        ],
        "property_chains": [
            {"chain_id": c.chain_id, "properties": c.properties, "implies": c.implies,
             "comment": c.comment}
            for c in ax.property_chains
        ],
        "defined_classes": [
            {"class_id": d.class_id, "label": d.label, "parent": d.parent,
             "expression": d.expression, "definition": d.definition,
             "severity": d.severity, "evidence_metrics": d.evidence_metrics}
            for d in ax.defined_classes
        ],
        "warnings": ax.warnings,
    }
    if args.format == "json":
        _emit(payload, "json")
        return 0
    print(f"公理层 v{ax.version}：类 {len(ax.classes)} / 对象属性 {len(ax.object_properties)}"
          f" / 属性链 {len(ax.property_chains)} / 数值定义类 {len(ax.defined_classes)}")
    for c in ax.classes:
        print(f"  [类] {c.label:<8} {c.class_id:<28} parent={c.parent}")
    for p in ax.object_properties:
        inv = f" inverse_of={p.inverse_of}" if p.inverse_of else ""
        print(f"  [属性] {p.label:<8} {p.property_id:<34} domain={p.domain} range={p.range}{inv}")
    for ch in ax.property_chains:
        print(f"  [链] {' ∘ '.join(ch.properties)} ⇒ {ch.implies}")
    for d in ax.defined_classes:
        print(f"  [规则] {d.label:<10} {d.class_id:<26} {d.expression}")
    for w in ax.warnings:
        print(f"  ! {w}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from finkb.ontology import export as export_mod

    try:
        manifest = export_mod.export(
            args.db, args.out, ontology_path=args.ontology,
            axioms_path=args.axioms, with_abox=not args.no_abox,
        )
    except ImportError as e:
        print(f"导出依赖缺失：{e}（pip install rdflib）")
        return 2
    except export_mod.ExportError as e:
        print(f"导出失败：{e}")
        return 2
    if args.format == "json":
        _emit(manifest, "json")
    else:
        print(f"TBox {manifest['tbox']['triples']} 三元组 → {args.out}/tbox.ttl")
        if manifest["abox"]:
            print(f"ABox {manifest['abox']['triples']} 三元组 → {args.out}/abox.ttl")
        for w in manifest.get("warnings") or []:
            print(f"  ! {w}")
    return 0


def cmd_infer(args: argparse.Namespace) -> int:
    from finkb.ontology import infer as infer_mod

    try:
        report = infer_mod.run(
            args.db, args.out, ontology_path=args.ontology,
            axioms_path=args.axioms, skip_owl=args.skip_owl,
        )
    except ImportError as e:
        print(f"推理依赖缺失：{e}（pip install rdflib owlrl）")
        return 2
    if args.format == "json":
        _emit(report, "json")
    else:
        d = report["defined_classes"]
        e = report["edges"]
        print(f"定义类命中 {d['hits']} 条："
              + ", ".join(f"{k.split(':')[-1]}={v}" for k, v in d["per_class"].items() if v))
        print(f"关系边：基础 {e['base_edges']}，推出 {e['inferred_edges']}（"
              + ", ".join(f"{k}={v}" for k, v in e["by_rule"].items()) + "）")
        if not report["owlrl"].get("skipped"):
            o = report["owlrl"]
            print(f"OWL 2 RL：TBox {o['tbox_triples']} + ABox {o['abox_triples']}"
                  f" = {o['base_triples']}，推出 {o['inferred_triples']} 三元组"
                  f"（{o['elapsed_s']}s）→ {o['out_file']}")
        else:
            print("OWL 2 RL：已跳过（--skip-owl）")
    return 0


def main(argv: list[str] | None = None) -> int:
    from finkb.fact_engine.selector import FINKB_ROOT

    ap = argparse.ArgumentParser(prog="finkb.ontology", description="FinKB 本体层")
    ap.add_argument("--db", default=str(FINKB_ROOT / "data" / "facts.duckdb"))
    ap.add_argument("--wiki", default=str(FINKB_ROOT / "data" / "wiki.duckdb"),
                    help="claims 账本库（用于链条第 7 跳）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("load", help="YAML → DuckDB")
    p.add_argument("--ontology", default=None)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("tree", help="打印四层概念树")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("audit", help="链条完整性体检：逐跳统计断点数")
    p.add_argument("--strict", action="store_true",
                   help="结构断点（概念/祖先链/约束/证据/推理血缘）非零即退出码 1，供门禁用")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_audit)
    p = sub.add_parser("chain", help="解析指标 → 概念 → 事实 → 证据 → 页面")
    p.add_argument("--metric", required=True)
    p.add_argument("--company", default=None)
    p.add_argument("--period", default=None)
    p.add_argument("--no-pages", action="store_true", help="跳过 claims 跳")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_chain)

    p = sub.add_parser("check", help="跑约束校验")
    p.add_argument("--tolerance", type=float, default=check.DEFAULT_TOL_PCT)
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("derive", help="算 L3 派生指标")
    p.add_argument("--company", required=True)
    p.add_argument("--period", required=True)
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_derive)

    p = sub.add_parser("concept", help="检索概念")
    p.add_argument("--id", default=None)
    p.add_argument("--query", default=None)
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_concept)

    p = sub.add_parser("dims", help="列出维度种类与成员（分产品/分行业/分地区）")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_dims)

    p = sub.add_parser("axioms", help="公理层：类/对象属性/属性链/数值定义类")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_axioms)

    p = sub.add_parser("export", help="YAML 本体 → TBox/ABox Turtle")
    p.add_argument("--out", default=str(FINKB_ROOT / "data" / "ontology"))
    p.add_argument("--ontology", default=None)
    p.add_argument("--axioms", default=None)
    p.add_argument("--no-abox", action="store_true", help="只导出 TBox")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("infer", help="定义类 + 属性链 + OWL 2 RL 闭包（物化推理结果）")
    p.add_argument("--out", default=str(FINKB_ROOT / "data" / "ontology"))
    p.add_argument("--ontology", default=None)
    p.add_argument("--axioms", default=None)
    p.add_argument("--skip-owl", action="store_true", help="只跑数值规则与属性链")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_infer)

    args = ap.parse_args(argv)
    if args.cmd == "concept" and not args.id and not args.query:
        ap.error("concept 需要 --id 或 --query")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
