"""用户本地知识库管理：导入年报、解析入库、建索引、删除、看状态。

设计边界：
- 代码与数据分离（``FINKB_HOME``），分发物不带任何语料；
- 每份文档有稳定 ``doc_id``：``{6位代码}_{期间}_{报告类型}``；
- 解析、事实抽取、wiki 编译、切块/索引全部复用现有流水线，不另写一套；
- 没有 LLM key 时只做「解析 + 切块」，事实/wiki 跳过并在结果里说明。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import yaml

from finkb.common import paths

COMPANY_RE = re.compile(r"^\d{6}$")
PERIOD_RE = re.compile(r"^20\d{2}(?:H1|Q1|Q3)?$")
DOC_ID_RE = re.compile(r"^\d{6}_20\d{2}_(?:年报|半年报|一季报|三季报)$")
TYPE_SUFFIX = {"年报": "", "半年报": "H1", "一季报": "Q1", "三季报": "Q3"}
REGISTRY_NAME = "library.json"
MANIFEST_HEADER = ["股票代码", "公司名称", "会计年度", "报告类型", "状态", "公告标题", "公告日期", "文件名", "大小MB", "链接", "备注"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _registry_path() -> Path:
    return paths.data_dir() / REGISTRY_NAME


def _read_registry() -> dict:
    path = _registry_path()
    if not path.exists():
        return {"version": 1, "documents": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "documents": {}}
    data.setdefault("documents", {})
    return data


def _write_registry(data: dict) -> None:
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    _registry_path().write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _scope_path() -> Path:
    return paths.config_dir() / "wiki_scope.yaml"


def _read_scope() -> dict:
    path = _scope_path()
    if not path.exists():
        return {"version": 1, "docs": []}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.setdefault("version", 1)
    raw.setdefault("docs", [])
    return raw


def _write_scope(scope: dict) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    _scope_path().write_text(
        yaml.safe_dump(scope, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _upsert_scope(entry: dict) -> None:
    scope = _read_scope()
    docs = [d for d in scope["docs"] if str(d.get("doc_id")) != entry["doc_id"]]
    docs.append({k: entry[k] for k in ("doc_id", "company", "company_name", "period", "report_type")})
    scope["docs"] = sorted(docs, key=lambda d: str(d["doc_id"]))
    _write_scope(scope)


def _remove_scope(doc_id: str) -> None:
    scope = _read_scope()
    scope["docs"] = [d for d in scope["docs"] if str(d.get("doc_id")) != doc_id]
    _write_scope(scope)


def _meta(company: str, period: str, report_type: str) -> dict:
    company = str(company).strip()
    period = str(period).strip().upper()
    report_type = str(report_type).strip()
    if not COMPANY_RE.match(company):
        raise ValueError(f"公司代码必须是 6 位数字：{company!r}")
    if report_type not in TYPE_SUFFIX:
        raise ValueError(f"报告类型必须是 {list(TYPE_SUFFIX)} 之一：{report_type!r}")
    if not PERIOD_RE.match(period):
        raise ValueError(f"期间格式应为 2025 / 2025H1 / 2025Q1 / 2025Q3：{period!r}")
    suffix = TYPE_SUFFIX[report_type]
    if suffix and not period.endswith(suffix):
        raise ValueError(f"{report_type} 的期间应以 {suffix} 结尾：{period!r}")
    if not suffix and len(period) != 4:
        raise ValueError(f"年报的期间应是四位年份：{period!r}")
    return {
        "doc_id": f"{company}_{period[:4]}_{report_type}",
        "company": company,
        "period": period,
        "report_type": report_type,
        "year": period[:4],
    }


def _documents() -> dict[str, dict]:
    docs = dict(_read_registry().get("documents") or {})
    if docs:
        return docs
    # 老仓库没有 registry 时，用 wiki_scope 兜底展示（只读，不写回）。
    out: dict[str, dict] = {}
    for d in _read_scope().get("docs") or []:
        doc_id = str(d.get("doc_id") or "")
        if not doc_id:
            continue
        out[doc_id] = {
            "doc_id": doc_id,
            "company": str(d.get("company") or ""),
            "company_name": str(d.get("company_name") or d.get("company") or ""),
            "period": str(d.get("period") or ""),
            "report_type": str(d.get("report_type") or ""),
            "added_at": "",
            "source": "existing",
        }
    return out


def list_documents() -> list[dict]:
    return [docs for _, docs in sorted(_documents().items())]


def _upsert_manifest(meta: dict, company_name: str) -> dict:
    """写 raw/manifest.csv 并返回 ingest 需要的 manifest dict。"""
    raw = paths.raw_dir()
    raw.mkdir(parents=True, exist_ok=True)
    path = raw / "manifest.csv"
    row = {
        "股票代码": meta["company"],
        "公司名称": company_name or meta["company"],
        "会计年度": meta["year"],
        "报告类型": meta["report_type"],
        "状态": "已导入",
        "公告标题": f"{meta['year']}年{meta['report_type']}",
        "公告日期": datetime.now().strftime("%Y-%m-%d"),
        "文件名": f"{meta['doc_id']}.pdf",
        "大小MB": "",
        "链接": "",
        "备注": "FinKB 本地库导入",
    }
    rows: list[dict] = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = [dict(r) for r in csv.DictReader(f)]
    key = (meta["company"], meta["year"], meta["report_type"])
    rows = [r for r in rows if (r.get("股票代码"), r.get("会计年度"), r.get("报告类型")) != key]
    rows.append(row)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_HEADER)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in MANIFEST_HEADER})
    return {key: row}


def _llm_client():
    from finkb.fact_engine.extractor import LLMClient, LLMConfig

    return LLMClient(LLMConfig.from_env())


def _run_parse(pdf: Path, doc_id: str, mode: str, warnings: list[str]) -> None:
    command = [
        sys.executable,
        "-m",
        "finkb.cli",
        "parse",
        str(pdf),
        "--doc-id",
        doc_id,
        "--out",
        str(paths.parsed_dir()),
        "--workdir",
        str(paths.data_dir() / "mineru_work"),
        "--split-sections",
    ]
    if mode in ("auto", "offline"):
        command.append("--offline")
        if mode == "auto":
            warnings.append("auto 解析使用离线兜底（不消耗 MinerU 额度）；需要高质量表格请传 parse_mode=mineru")
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError("解析失败：" + " / ".join(tail))
    if not (paths.parsed_dir() / doc_id / "report.json").exists():
        raise RuntimeError(f"解析产物不完整：{paths.parsed_dir() / doc_id}")


def _rebuild_indexes(embed: bool) -> dict:
    from finkb.search import chunker, hybrid

    chunk_stats = chunker.build(
        parsed_root=paths.parsed_dir(),
        out_path=paths.chunks_path(),
        scope_path=_scope_path(),
    )
    embedder = None
    warnings: list[str] = []
    if embed:
        try:
            from finkb.search.embed import SiliconFlowEmbedder

            embedder = SiliconFlowEmbedder()
        except Exception as exc:  # noqa: BLE001 - 没有 key 就只建关键词索引
            warnings.append(f"跳过向量索引：{exc}")
    index_stats = hybrid.build_index(
        chunks_path=paths.chunks_path(),
        db_path=paths.chunks_db(),
        vectors_path=paths.vectors_path(),
        embedder=embedder,
    )
    if not embed and paths.vectors_path().exists():
        paths.vectors_path().unlink()
        warnings.append("切块已变化，已删除旧向量索引；需要向量检索请用 rebuild --embed")
    return {"chunks": chunk_stats, "index": index_stats, "warnings": warnings}


def init_library() -> dict:
    paths.ensure_home()
    return {"ok": True, "home": str(paths.HOME), "status": status()}


def status() -> dict:
    home = paths.HOME
    initialized = home.exists() and paths.data_dir().exists()
    docs = _documents()
    info: dict = {
        "home": str(home),
        "initialized": initialized,
        "custom_home": paths.is_custom_home(),
        "documents": len(docs),
        "config_files": sorted(p.name for p in paths.config_dir().glob("*.yaml")) if paths.config_dir().exists() else [],
        "llm_ready": False,
        "mineru_ready": False,
        "counts": {},
        "index": {},
    }
    env = paths.env_file()
    if env.exists():
        text = env.read_text(encoding="utf-8", errors="ignore")
        info["llm_ready"] = "LLM_API_KEY=" in text and not re.search(r"LLM_API_KEY=\s*$", text, re.M)
        info["mineru_ready"] = "MINERU_API_TOKEN=" in text and not re.search(r"MINERU_API_TOKEN=\s*$", text, re.M)
    if paths.facts_db().exists():
        try:
            con = duckdb.connect(str(paths.facts_db()), read_only=True)
            try:
                for table in ("facts", "relations", "entities_master", "note_anchors", "inferred_concepts"):
                    try:
                        info["counts"][table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    except duckdb.Error:
                        info["counts"][table] = None
            finally:
                con.close()
        except duckdb.Error:
            pass
    chunks = paths.chunks_path()
    if chunks.exists():
        info["index"]["chunks"] = sum(1 for _ in chunks.open(encoding="utf-8"))
    vectors = paths.vectors_path()
    info["index"]["vectors"] = vectors.stat().st_size if vectors.exists() else 0
    vault = paths.wiki_vault_dir()
    if vault.exists():
        info["index"]["wiki_pages"] = sum(1 for p in vault.rglob("*.md") if p.name != "index.md")
    return info


def add_document(
    *,
    company: str,
    period: str,
    report_type: str,
    pdf_path: str | Path | None = None,
    parsed_dir: str | Path | None = None,
    company_name: str | None = None,
    doc_id: str | None = None,
    parse_mode: str = "auto",
    skip_facts: bool = False,
    skip_wiki: bool = False,
    embed: bool = False,
    workers: int = 1,
) -> dict:
    paths.ensure_home()
    meta = _meta(company, period, report_type)
    doc_id = str(doc_id or meta["doc_id"]).strip()
    if not DOC_ID_RE.match(doc_id) or doc_id != meta["doc_id"]:
        raise ValueError(f"doc_id 与公司/期间/类型不一致：{doc_id!r}")
    target = paths.parsed_dir() / doc_id
    warnings: list[str] = []
    raw_pdf: Path | None = None

    if parsed_dir:
        src = Path(parsed_dir).expanduser().resolve()
        if not (src / "report.json").exists():
            raise FileNotFoundError(f"解析目录缺少 report.json：{src}")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target)
        if pdf_path:
            raw_pdf = paths.raw_dir() / f"{doc_id}.pdf"
            shutil.copy2(Path(pdf_path).expanduser().resolve(), raw_pdf)
    else:
        if pdf_path:
            src_pdf = Path(pdf_path).expanduser().resolve()
            if not src_pdf.exists():
                raise FileNotFoundError(f"PDF 不存在：{src_pdf}")
            raw_pdf = paths.raw_dir() / f"{doc_id}.pdf"
            shutil.copy2(src_pdf, raw_pdf)
        if not target.exists():
            if raw_pdf is None:
                raise FileNotFoundError("需要提供 pdf_path（导入 PDF）或 parsed_dir（导入已解析产物）")
            _run_parse(raw_pdf, doc_id, parse_mode, warnings)

    manifest = _upsert_manifest(meta, company_name or meta["company"])
    company_name = company_name or meta["company"]

    if not skip_facts:
        try:
            with _llm_client() as client:
                from finkb.fact_engine.ingest import company_units, ingest_documents
                from finkb.fact_engine.metric_dict import load_metric_dict
                from finkb.fact_engine.store import FactStore

                metrics = load_metric_dict()
                units = company_units([target], manifest)
                with FactStore(paths.facts_db()) as store:
                    ingest_documents(store, client, [target], metrics, manifest, units, workers=workers)
        except RuntimeError as exc:
            warnings.append(f"跳过事实抽取：{exc}")
        except Exception as exc:  # noqa: BLE001 - 单份文档失败不破坏已有库
            warnings.append(f"事实抽取失败：{type(exc).__name__}: {exc}")

    if not skip_wiki:
        try:
            with _llm_client() as client:
                from finkb.fact_engine.store import FactStore
                from finkb.wiki.compiler import compile_document
                from finkb.wiki.vault import init_vault, rebuild_index

                with FactStore(paths.facts_db()) as store:
                    compile_document(
                        client,
                        store,
                        doc_id,
                        company=company_name,
                        period=meta["period"],
                        vault_root=paths.wiki_vault_dir(),
                        claims_db=paths.wiki_db(),
                        data_root=paths.data_dir(),
                    )
                root = init_vault(paths.wiki_vault_dir())
                rebuild_index(root)
        except RuntimeError as exc:
            warnings.append(f"跳过 wiki 编译：{exc}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"wiki 编译失败：{type(exc).__name__}: {exc}")

    _upsert_scope({
        "doc_id": doc_id,
        "company": meta["company"],
        "company_name": company_name,
        "period": meta["period"],
        "report_type": meta["report_type"],
    })
    index = _rebuild_indexes(embed)
    warnings.extend(index.get("warnings") or [])

    registry = _read_registry()
    registry["documents"][doc_id] = {
        "doc_id": doc_id,
        "company": meta["company"],
        "company_name": company_name,
        "period": meta["period"],
        "report_type": meta["report_type"],
        "added_at": _now(),
        "source": "parsed" if parsed_dir else "pdf",
        "warnings": warnings,
    }
    _write_registry(registry)
    return {
        "ok": True,
        "doc_id": doc_id,
        "home": str(paths.HOME),
        "chunks": index.get("chunks"),
        "warnings": warnings,
    }


def remove_document(doc_id: str) -> dict:
    doc_id = str(doc_id).strip()
    if not DOC_ID_RE.match(doc_id):
        raise ValueError(f"doc_id 格式不正确：{doc_id!r}")
    removed: dict[str, int] = {}
    target = paths.parsed_dir() / doc_id
    if target.exists():
        shutil.rmtree(target)
        removed["parsed_dir"] = 1
    raw_pdf = paths.raw_dir() / f"{doc_id}.pdf"
    if raw_pdf.exists():
        raw_pdf.unlink()
        removed["raw_pdf"] = 1
    if paths.facts_db().exists():
        con = duckdb.connect(str(paths.facts_db()))
        try:
            con.execute("BEGIN")
            for table, column in (
                ("facts", "source_doc"),
                ("relations", "source_doc"),
                ("segment_facts", "source_doc"),
                ("note_anchors", "doc_id"),
                ("conflicts", "doc_a"),
                ("conflicts", "doc_b"),
            ):
                try:
                    n = con.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", [doc_id]
                    ).fetchone()[0]
                    if n:
                        con.execute(f"DELETE FROM {table} WHERE {column} = ?", [doc_id])
                        removed[f"{table}.{column}"] = n
                except duckdb.Error:
                    continue
            con.execute("COMMIT")
        finally:
            con.close()
    if paths.wiki_db().exists():
        con = duckdb.connect(str(paths.wiki_db()))
        try:
            con.execute("DELETE FROM claims WHERE page LIKE ?", [f"%{doc_id}%"])
        except duckdb.Error:
            pass
        finally:
            con.close()
    vault = paths.wiki_vault_dir()
    if vault.exists():
        for p in vault.rglob(f"{doc_id}*"):
            if p.is_file():
                p.unlink()
                removed["wiki_pages"] = removed.get("wiki_pages", 0) + 1
    _remove_scope(doc_id)
    # 实体主数据与附注锚点依赖全量事实/关系，删完要重算，避免留下已删公司的实体。
    if paths.facts_db().exists():
        try:
            from finkb.fact_engine.entity_registry import annotate, rebuild
            from finkb.fact_engine.notes import build_note_anchors
            from finkb.fact_engine.store import FactStore

            with FactStore(paths.facts_db()) as store:
                rebuild(store, scope_path=_scope_path())
                annotate(store)
                if paths.parsed_dir().exists():
                    build_note_anchors(store, paths.parsed_dir())
        except Exception as exc:  # noqa: BLE001 - 重算失败不影响文档删除
            removed["post_cleanup_error"] = 1
    registry = _read_registry()
    if registry["documents"].pop(doc_id, None) is not None:
        _write_registry(registry)
        removed["registry"] = 1
    index = _rebuild_indexes(embed=False)
    return {"ok": True, "doc_id": doc_id, "removed": removed, "chunks": index.get("chunks"),
            "warnings": index.get("warnings") or []}


def rebuild_indexes(embed: bool = False) -> dict:
    paths.ensure_home()
    index = _rebuild_indexes(embed)
    return {"ok": True, "home": str(paths.HOME), **index}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m finkb.library", description="FinKB 本地知识库管理")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化本地库目录与配置模板")
    sub.add_parser("status", help="查看本地库状态")
    sub.add_parser("list", help="列出已导入文档")
    add = sub.add_parser("add", help="导入一份年报")
    add.add_argument("--pdf", help="PDF 路径")
    add.add_argument("--parsed", help="已解析产物目录（含 report.json）")
    add.add_argument("--company", required=True, help="6 位股票代码")
    add.add_argument("--period", required=True, help="2025 / 2025H1 / 2025Q1 / 2025Q3")
    add.add_argument("--type", dest="report_type", required=True, choices=list(TYPE_SUFFIX))
    add.add_argument("--name", help="公司简称")
    add.add_argument("--parse-mode", default="auto", choices=("auto", "offline", "mineru"))
    add.add_argument("--no-facts", action="store_true")
    add.add_argument("--no-wiki", action="store_true")
    add.add_argument("--embed", action="store_true")
    remove = sub.add_parser("remove", help="删除一份文档及其数据")
    remove.add_argument("doc_id")
    rebuild = sub.add_parser("rebuild", help="重建切块与检索索引")
    rebuild.add_argument("--embed", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "init":
        print(json.dumps(init_library(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "list":
        print(json.dumps(list_documents(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "add":
        result = add_document(
            company=args.company, period=args.period, report_type=args.report_type,
            pdf_path=args.pdf, parsed_dir=args.parsed, company_name=args.name,
            parse_mode=args.parse_mode, skip_facts=args.no_facts, skip_wiki=args.no_wiki,
            embed=args.embed,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "remove":
        print(json.dumps(remove_document(args.doc_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "rebuild":
        print(json.dumps(rebuild_indexes(args.embed), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
