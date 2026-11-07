"""最小 MCP stdio server（P2）：把三档问答/指标/检索/本体/关系暴露给 agent。

协议：newline-delimited JSON-RPC 2.0（MCP stdio transport，2024-11-05）。
只做协议适配，不复制业务逻辑；数字仍由 SQL 计算（ADR-001）。
"""

from __future__ import annotations

import json
import sys

import duckdb

from finkb.fact_engine.selector import FINKB_ROOT
from finkb import library
from finkb.qa import evidence as evidence_mod
from finkb.qa.pipeline import ask as ask_pipeline
from finkb.qa.synth import normalize_response
from finkb.qa.synth_verify import verify_synthesis
from finkb.search import hybrid

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "finkb-qa", "version": "0.1.0"}

FACT_DB = FINKB_ROOT / "data" / "facts.duckdb"
CHUNKS_PATH = FINKB_ROOT / "data" / "_search" / "chunks.jsonl"
CHUNKS_DB = FINKB_ROOT / "data" / "_search" / "chunks.db"

TOOLS = [
    {
        "name": "init_library",
        "description": "初始化本地知识库目录（data/config/raw/wiki_vault），幂等；首次使用先调这个。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "library_status",
        "description": "查看本地知识库状态：目录、文档数、事实/关系/切块/向量计数、LLM 与 MinerU 是否就绪。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_documents",
        "description": "列出本地知识库已导入的文档（doc_id/公司/期间/报告类型）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_document",
        "description": "导入一份 A 股年报：传 PDF（解析后入库）或已解析目录（直接入库）；随后建切块与检索索引。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "6 位股票代码，如 600519"},
                "period": {"type": "string", "description": "2025 / 2025H1 / 2025Q1 / 2025Q3"},
                "report_type": {"type": "string", "enum": ["年报", "半年报", "一季报", "三季报"]},
                "pdf_path": {"type": "string", "description": "本机 PDF 路径"},
                "parsed_dir": {"type": "string", "description": "已解析产物目录（含 report.json）"},
                "company_name": {"type": "string"},
                "parse_mode": {"type": "string", "enum": ["auto", "offline", "mineru"]},
                "skip_facts": {"type": "boolean", "description": "只解析+切块，不跑 LLM 事实抽取"},
                "skip_wiki": {"type": "boolean", "description": "不编译 wiki 页面"},
                "embed": {"type": "boolean", "description": "同时建向量索引（需要 SILICONFLOW_API_KEY）"},
            },
            "required": ["company", "period", "report_type"],
        },
    },
    {
        "name": "remove_document",
        "description": "删除一份文档及其事实/关系/wiki/索引数据（不可恢复）。",
        "inputSchema": {
            "type": "object",
            "properties": {"doc_id": {"type": "string", "description": "如 600519_2025_年报"}},
            "required": ["doc_id"],
        },
    },
    {
        "name": "rebuild_index",
        "description": "重建全部切块与检索索引（可选同时重建向量索引）。",
        "inputSchema": {
            "type": "object",
            "properties": {"embed": {"type": "boolean", "description": "重建向量索引（需要 key）"}},
        },
    },
    {
        "name": "ask",
        "description": "三档问答（metric 指标 / ontology 本体 / document 文档），返回答案、档位、置信度与引用。可传上一轮 context 做多轮追问。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "自然语言问题"},
                "tier": {"type": "string", "enum": ["auto", "metric", "ontology", "document"]},
                "context": {
                    "type": "object",
                    "description": "上一轮结构化槽位：{previous_question?, previous_slots?: {companies, company_names, periods, metric, qualifiers}}；项目只做确定性继承，不保存会话。",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "compare",
        "description": "跨期对比：同一公司同一指标多期间并排返回数值、单位与引用；数字由事实库 SQL 计算。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "自然语言问题，如：美的集团 2023-2025 年营业收入"},
                "periods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选期间列表（2023 / 2024年 / 2024H1）；不给则从问题解析",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "query_metric",
        "description": "按公司代码/期间/指标 id 查一个精确指标值（Tier 1 底层查询）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "6 位股票代码，如 002594"},
                "period": {"type": "string", "description": "期间，如 2025 / 2025H1 / 2025Q1"},
                "metric_id": {"type": "string", "description": "指标 id，如 revenue"},
            },
            "required": ["company", "period", "metric_id"],
        },
    },
    {
        "name": "search_documents",
        "description": "在原文块索引中检索（FTS，可加公司/期间/文档/块类型过滤）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "company": {"type": "string"},
                "period": {"type": "string"},
                "doc_id": {"type": "string"},
                "block_type": {"type": "string", "enum": ["text", "table"]},
            },
            "required": ["query"],
        },
    },
    {
        "name": "describe_ontology",
        "description": "查看本体结构：概念、关系、定义类；传 concept_id 看单个概念及其边。",
        "inputSchema": {
            "type": "object",
            "properties": {"concept_id": {"type": "string"}},
        },
    },
    {
        "name": "traverse_relations",
        "description": "从实体出发查关系边（子公司/担保/股东/关联方），支持按关系类型过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "公司代码或实体名"},
                "rel_type": {
                    "type": "string",
                    "enum": ["subsidiary_of", "guarantees_for", "shareholder_of", "related_party_of"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["entity"],
        },
    },
    {
        "name": "evidence",
        "description": "取回答所需的证据包（事实/推理/原文块/引用），供宿主自己写答案（模式 B）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "自然语言问题"},
                "tier": {"type": "string", "enum": ["auto", "metric", "ontology", "document"]},
            },
            "required": ["question"],
        },
    },
    {
        "name": "verify",
        "description": "校验宿主写好的答案（数字占位符、数字绑定、引用页码、句级引用、白名单、拒答 missing）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "自然语言问题（用于重建证据包）"},
                "tier": {"type": "string", "enum": ["auto", "metric", "ontology", "document"]},
                "answer": {
                    "type": "object",
                    "description": "宿主生成的答案对象",
                    "properties": {
                        "answer": {"type": "string"},
                        "used_ids": {"type": "array", "items": {"type": "string"}},
                        "refused": {"type": "boolean"},
                        "missing": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["answer"],
                },
            },
            "required": ["question", "answer"],
        },
    },
]


def _connect() -> duckdb.DuckDBPyConnection:
    if not FACT_DB.exists():
        raise FileNotFoundError(f"事实库不存在：{FACT_DB}")
    return duckdb.connect(str(FACT_DB), read_only=True)


def call_tool(name: str, args: dict) -> dict:
    if name == "init_library":
        return library.init_library()
    if name == "library_status":
        return library.status()
    if name == "list_documents":
        return {"documents": library.list_documents()}
    if name == "add_document":
        return library.add_document(
            company=str(args["company"]),
            period=str(args["period"]),
            report_type=str(args["report_type"]),
            pdf_path=args.get("pdf_path"),
            parsed_dir=args.get("parsed_dir"),
            company_name=args.get("company_name"),
            parse_mode=str(args.get("parse_mode", "auto")),
            skip_facts=bool(args.get("skip_facts")),
            skip_wiki=bool(args.get("skip_wiki")),
            embed=bool(args.get("embed")),
        )
    if name == "remove_document":
        return library.remove_document(str(args["doc_id"]))
    if name == "rebuild_index":
        return library.rebuild_indexes(bool(args.get("embed")))
    if name == "ask":
        return ask_pipeline(
            str(args["question"]),
            tier=args.get("tier", "auto"),
            context=args.get("context"),
        )
    if name == "compare":
        from finkb.qa.compare import compare as compare_qa

        return compare_qa(str(args["question"]), periods=args.get("periods"))
    if name == "query_metric":
        con = _connect()
        try:
            row = con.execute(
                "SELECT company, period, metric_id, value_norm, norm_unit, source_doc, page_num "
                "FROM facts_dedup WHERE company = ? AND period = ? AND metric_id = ? "
                "ORDER BY source_doc LIMIT 1",
                [args["company"], args["period"], args["metric_id"]],
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return {"found": False, "message": "没有匹配的事实（公司/期间/指标可能不在语料内）"}
        return {"found": True, "company": row[0], "period": row[1], "metric_id": row[2],
                "value": row[3], "unit": row[4], "source_doc": row[5], "page_num": row[6]}
    if name == "search_documents":
        chunks = hybrid.load_chunks(CHUNKS_PATH)
        filters = {key: args[key] for key in ("company", "period", "doc_id", "block_type") if args.get(key)}
        return {
            "results": hybrid.search(
                str(args["query"]), limit=int(args.get("limit", 5)), chunks=chunks,
                db_path=CHUNKS_DB, filters=filters or None,
            )
        }
    if name == "describe_ontology":
        con = _connect()
        try:
            concept_id = args.get("concept_id")
            if concept_id:
                concept = con.execute(
                    "SELECT concept_id, label, layer, domain, definition, metric_id FROM concepts WHERE concept_id = ?",
                    [concept_id],
                ).fetchone()
                if concept is None:
                    return {"found": False, "message": f"概念不存在：{concept_id}"}
                edges = con.execute(
                    "SELECT src_concept, rel_type, dst_concept FROM concept_relations "
                    "WHERE src_concept = ? OR dst_concept = ? ORDER BY rel_type, dst_concept",
                    [concept_id, concept_id],
                ).fetchall()
                return {"found": True, "concept": {"concept_id": concept[0], "label": concept[1],
                                                   "layer": concept[2], "domain": concept[3],
                                                   "definition": concept[4], "metric_id": concept[5]},
                        "relations": [{"src": s, "rel_type": r, "dst": d} for s, r, d in edges]}
            counts = {
                "concepts": con.execute("SELECT COUNT(*) FROM concepts").fetchone()[0],
                "relations": con.execute("SELECT COUNT(*) FROM concept_relations").fetchone()[0],
                "defined_classes": con.execute("SELECT COUNT(*) FROM ontology_defined_classes").fetchone()[0],
            }
            layers = dict(con.execute("SELECT layer, COUNT(*) FROM concepts GROUP BY layer ORDER BY layer").fetchall())
            classes = [row[0] for row in con.execute(
                "SELECT class_id FROM ontology_defined_classes ORDER BY class_id").fetchall()]
            return {"counts": counts, "layers": layers, "defined_classes": classes}
        finally:
            con.close()
    if name in ("evidence", "verify"):
        envelope = ask_pipeline(str(args["question"]), tier=args.get("tier", "auto"), synthesize=False)
        pack = evidence_mod.build_pack(str(args["question"]), envelope)
        if name == "evidence":
            return {
                "mode": "B",
                "responsibility": "答案文本由宿主生成；FinKB 只提供证据包与校验工具，不对宿主生成的表述负责。",
                "tier": envelope.get("tier"),
                "refused": envelope.get("refused", False),
                "evidence": pack.to_dict(),
            }
        result = normalize_response(dict(args["answer"]), pack)
        verdict = verify_synthesis(result, pack).to_dict()
        verdict["mode"] = "B"
        verdict["responsibility"] = "答案文本由宿主生成；FinKB 只提供证据包与校验工具，不对宿主生成的表述负责。"
        return verdict
    if name == "traverse_relations":
        con = _connect()
        try:
            entity = str(args["entity"])
            entity_row = con.execute(
                "SELECT entity_id, canonical_name FROM entities_master "
                "WHERE canonical_name = ? OR entity_id = ? LIMIT 1",
                [entity, f"co:{entity}"],
            ).fetchone()
            if entity_row is None:
                alias = con.execute(
                    "SELECT entity_id, canonical_name FROM entity_aliases a "
                    "JOIN entities_master m USING (entity_id) WHERE a.alias = ? LIMIT 1",
                    [entity],
                ).fetchone()
                entity_row = alias
            if entity_row is None:
                return {"found": False, "message": f"实体未登记：{entity}"}
            entity_id, canonical = entity_row
            sql = (
                "SELECT src_id, rel_type, dst_id, source_doc, page_num, attrs_json FROM relations "
                "WHERE (src_entity_id = ? OR dst_entity_id = ?)"
            )
            params = [entity_id, entity_id]
            if args.get("rel_type"):
                sql += " AND rel_type = ?"
                params.append(args["rel_type"])
            sql += " ORDER BY rel_type, dst_id LIMIT ?"
            params.append(int(args.get("limit", 20)))
            rows = con.execute(sql, params).fetchall()
            return {"found": True, "entity_id": entity_id, "canonical_name": canonical,
                    "edges": [{"src": r[0], "rel_type": r[1], "dst": r[2], "source_doc": r[3],
                               "page_num": r[4]} for r in rows]}
        finally:
            con.close()
    raise KeyError(f"未知工具：{name}")


def _ok(request_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(request: dict) -> dict | None:
    """处理一条 JSON-RPC 请求；通知（无 id）返回 None。"""
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return _ok(request_id, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                                "serverInfo": SERVER_INFO})
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _ok(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params") or {}
        try:
            payload = call_tool(params.get("name"), params.get("arguments") or {})
        except KeyError as exc:
            return _error(request_id, -32602, str(exc))
        except Exception as exc:  # noqa: BLE001 - MCP 侧统一按工具错误返回
            return _ok(request_id, {"content": [{"type": "text", "text": json.dumps(
                {"error": str(exc)}, ensure_ascii=False)}], "isError": True})
        return _ok(request_id, {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                                "isError": False})
    if request_id is None:
        return None
    return _error(request_id, -32601, f"未知方法：{method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(_error(None, -32700, "JSON 解析失败")), flush=True)
            continue
        response = handle(request)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
