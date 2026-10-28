"""FinKB 命令行入口。

用法::

    # 只解析指定页（推荐先用这个验证，省额度）
    python -m finkb.cli parse "../raw/002594_2025_年报.pdf" --pages 43-44

    # 全量解析
    python -m finkb.cli parse "../raw/002594_2025_年报.pdf"

    # 离线模式（pypdf 兜底，不消耗额度，用于验证链路）
    python -m finkb.cli parse "../raw/002594_2025_年报.pdf" --offline

产物落在 ``data/parsed/<doc_id>/``::

    page_anchored.md      页码锚定 Markdown（L0 落盘格式）
    content_list.json     MinerU 原始输出（有则保存）
    tables/T001.md        每张逻辑表一张文件，便于人工核对
    report.json           统计与缝合审计记录
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from finkb.common.models import StitchedTable
from finkb.version import pipeline_fingerprint
from finkb.parser.mineru_client import (
    DEFAULT_PAGE_LIMIT,
    MineruClient,
    chunk_ranges,
    content_list_to_pages,
    detect_page_offset,
    shift_page_idx,
    MineruConfig,
    MineruError,
    OfflinePdfParser,
    ParsedDocument,
    pdf_page_count,
    render_page_anchored_markdown,
)
from finkb.parser.noise_filter import strip_running_noise
from finkb.parser.section_splitter import SplitOptions, split_all
from finkb.parser.table_stitcher import stitch_tables

FINKB_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = FINKB_ROOT / "config" / ".env"
DEFAULT_DATA_DIR = FINKB_ROOT / "data"


def load_env_file(path: Path) -> dict[str, str]:
    """解析 ``.env`` 文件为字典。

    刻意**不引入 python-dotenv**：格式足够简单，自己解析 20 行即可，
    少一个依赖就少一处离线环境风险。支持 ``#`` 注释、``export`` 前缀、
    以及值两侧的单/双引号。
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def apply_env_file(path: Path) -> dict[str, str]:
    """把 ``.env`` 注入 ``os.environ``；**不覆盖**已存在的变量。

    不覆盖是有意的：这样 ``MINERU_API_TOKEN=xxx python -m finkb.cli ...``
    这种临时覆盖能生效，便于多 key 切换。
    """
    values = load_env_file(path)
    for key, value in values.items():
        if value and key not in os.environ:
            os.environ[key] = value
    return values


def _doc_id_from_path(pdf_path: Path) -> str:
    return pdf_path.stem


def _serialize_table(table: StitchedTable) -> dict:
    return {
        "parent_id": table.parent_id,
        "table_id": table.table_id,
        "doc_id": table.doc_id,
        "section": table.section,
        "page_start": table.page_start,
        "page_end": table.page_end,
        "source_tables": table.source_tables,
        "is_stitched": table.is_stitched,
        "ncols": table.ncols,
        "header": table.header,
        "row_count": len(table.rows),
        "rows": table.rows,
    }


def run_parse(args: argparse.Namespace) -> int:
    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        print(f"[错误] 找不到 PDF：{pdf_path}", file=sys.stderr)
        return 2

    env_file = Path(args.env_file).expanduser().resolve()
    apply_env_file(env_file)

    doc_id = args.doc_id or _doc_id_from_path(pdf_path)
    output_dir = Path(args.out).expanduser().resolve() / doc_id
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"PDF      : {pdf_path}")
    print(f"doc_id   : {doc_id}")
    print(f"输出目录 : {output_dir}")
    print(f"配置文件 : {env_file} ({'存在' if env_file.exists() else '不存在'})")

    # ── 1. 解析 ────────────────────────────────────────────────
    reuse_path = getattr(args, "reuse_content_list", None)
    if reuse_path:
        # 复用已保存的 content_list.json 重跑后处理：改清洗/缝合逻辑时
        # 不必重新调用 MinerU，既省额度又能快速验证修复效果。
        reuse_file = Path(reuse_path).expanduser().resolve()
        content_list = json.loads(reuse_file.read_text(encoding="utf-8"))
        offset, offset_source = detect_page_offset(content_list, pdf_path)
        if offset:
            content_list = shift_page_idx(content_list, offset)
        document = ParsedDocument(
            doc_id=doc_id,
            pages=content_list_to_pages(content_list, doc_id),
            markdown="",
            content_list=content_list,
            source="mineru-reuse",
            page_offset=offset,
        )
        print(f"模式     : 复用已有解析结果 {reuse_file}")
        print(f"页码校准 : {offset:+d} 页（{offset_source}）")
    elif args.offline:
        print("模式     : 离线（pypdf 兜底，无表格结构）")
        document: ParsedDocument = OfflinePdfParser(doc_id).parse(pdf_path)
    else:
        try:
            config = MineruConfig.from_env()
        except MineruError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 3
        ranges = args.pages or None
        print(f"模式     : MinerU 在线 ({config.base_url}, {config.model_version})")
        client = MineruClient(config)
        workdir = Path(args.workdir).expanduser().resolve()
        limit = int(getattr(args, "max_pages", DEFAULT_PAGE_LIMIT))
        auto_split = bool(getattr(args, "auto_split", False)) and ranges is None
        if auto_split:
            chunks = chunk_ranges(pdf_page_count(pdf_path), limit)
            if len(chunks) > 1:
                print(f"分批     : {len(chunks)} 批（上限 {limit} 页）"
                      f" " + "、".join(f"{a}-{b}" for a, b in chunks))
        print(f"页码范围 : {ranges or '全部'}")
        try:
            if auto_split:
                document = client.parse_pdf_split(pdf_path, doc_id, workdir, limit)
            else:
                document = client.parse_pdf(
                    pdf_path, doc_id, workdir, page_ranges=ranges
                )
        except MineruError as exc:
            print(f"[错误] MinerU 解析失败：{exc}", file=sys.stderr)
            return 4

    print(f"解析结果 : {len(document.pages)} 页")

    # ── 2. 清洗 + 缝合 ─────────────────────────────────────────
    noise = strip_running_noise(document.pages)
    stitch = stitch_tables(document.pages)

    print(f"噪音清洗 : 剥离 {noise.removed_lines} 行，覆盖 {len(noise.affected_pages)} 页")
    print(f"逻辑表   : {len(stitch.tables)} 张，其中跨页缝合 {stitch.stitched_count} 张")

    # ── 3. 落盘 ────────────────────────────────────────────────
    (output_dir / "page_anchored.md").write_text(
        render_page_anchored_markdown(document.pages), encoding="utf-8"
    )
    if document.content_list:
        (output_dir / "content_list.json").write_text(
            json.dumps(document.content_list, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    logical_tables = []
    if getattr(args, "split_sections", False):
        logical_tables = split_all(stitch.tables, SplitOptions())
        print(f"逻辑表   : {len(logical_tables)} 张"
              f"（物理表 {len(stitch.tables)} → 切分净增 {len(logical_tables) - len(stitch.tables)}）")
        from collections import Counter
        print(f"  类型分布: {dict(Counter(t.kind for t in logical_tables))}")
        print(f"  继承表头: {sum(1 for t in logical_tables if t.header_source == 'inherited' and t.header)} 张")
        logical_dir = output_dir / "logical_tables"
        logical_dir.mkdir(exist_ok=True)
        for seq, logical in enumerate(logical_tables, start=1):
            # 序号前缀避免不同物理表的 S01 互相覆盖
            name = f"{seq:04d}_{logical.logical_id.split('#')[-1]}"
            (logical_dir / f"{name}.md").write_text(
                f"<!-- {logical.logical_id}  kind={logical.kind}  "
                f"header_source={logical.header_source}  "
                f"section={'/'.join(logical.section_path) or '—'}"
                + (f"  period={logical.period_label}" if logical.period_label else "")
                + (f"  caption={logical.caption}" if logical.caption else "")
                + " -->\n"
                f"{logical.to_markdown()}\n",
                encoding="utf-8",
            )

    tables_dir = output_dir / "tables"
    tables_dir.mkdir(exist_ok=True)
    for index, table in enumerate(stitch.tables, start=1):
        (tables_dir / f"T{index:03d}.md").write_text(
            f"<!-- {table.parent_id}  P{table.page_start}-P{table.page_end}  "
            f"{table.source_tables} tables -->\n{table.to_markdown()}\n",
            encoding="utf-8",
        )

    report = {
        "doc_id": doc_id,
        "pipeline_fingerprint": pipeline_fingerprint(),
        "source": document.source,
        "pdf": str(pdf_path),
        "pages": len(document.pages),
        "noise": {
            "signatures": sorted(noise.signatures),
            "removed_lines": noise.removed_lines,
            "affected_pages": noise.affected_pages,
        },
        "tables": [_serialize_table(table) for table in stitch.tables],
        "stitch_records": [
            {"pages": record.pages, "ncols": record.ncols, "reason": record.reason}
            for record in stitch.records
        ],
        "logical_tables": [
            {
                "logical_id": t.logical_id,
                "parent_id": t.parent_id,
                "kind": t.kind,
                "header_source": t.header_source,
                "section_path": t.section_path,
                "header": t.header,
                "row_count": len(t.rows),
                "row_range": list(t.row_range),
            }
            for t in logical_tables
        ],
        "stitch_rejections": [
            {"pages": record.pages, "ncols": record.ncols, "reason": record.reason}
            for record in stitch.rejections
        ],
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n已写入 {output_dir}")

    # ── 4. 缝合结果摘要 ────────────────────────────────────────
    if stitch.records:
        print("\n跨页缝合明细：")
        for record in stitch.records:
            pages = "→".join(f"P{p}" for p in record.pages)
            print(f"  {pages}  {record.ncols} 列  ({record.reason})")
    else:
        print("\n未发生跨页缝合。")

    if stitch.rejections:
        print("\n⚠️ 疑似跨页续接但被判据拒绝（请人工核对）：")
        for record in stitch.rejections:
            pages = "→".join(f"P{p}" for p in record.pages)
            print(f"  {pages}  {record.reason}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finkb", description="FinKB 命令行工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_cmd = subparsers.add_parser("parse", help="解析 PDF → 页码锚定 MD + 逻辑表")
    parse_cmd.add_argument("pdf", help="PDF 文件路径")
    parse_cmd.add_argument("--doc-id", help="文档主键，缺省取文件名主干")
    parse_cmd.add_argument(
        "--pages",
        help="只解析指定页（如 43-44），**强烈建议首次联调使用**，可大幅节省额度",
    )
    parse_cmd.add_argument(
        "--offline", action="store_true", help="离线模式（pypdf 兜底，不调用 MinerU）"
    )
    parse_cmd.add_argument(
        "--auto-split",
        dest="auto_split",
        action="store_true",
        help="超过 MinerU 单文件页数上限时自动分批解析并合并成一份 content_list",
    )
    parse_cmd.add_argument(
        "--max-pages",
        dest="max_pages",
        type=int,
        default=DEFAULT_PAGE_LIMIT,
        help=f"分批时每批最多多少页（默认 {DEFAULT_PAGE_LIMIT}）",
    )
    parse_cmd.add_argument(
        "--split-sections",
        dest="split_sections",
        action="store_true",
        help="在物理表基础上切分出逻辑表（含表头继承），输出到 logical_tables/",
    )
    parse_cmd.add_argument(
        "--reuse-content-list",
        dest="reuse_content_list",
        help="复用已保存的 content_list.json，只重跑清洗与缝合（不消耗 MinerU 额度）",
    )
    parse_cmd.add_argument(
        "--env-file", default=str(DEFAULT_ENV_FILE), help="配置文件路径"
    )
    parse_cmd.add_argument(
        "--out", default=str(DEFAULT_DATA_DIR / "parsed"), help="输出根目录"
    )
    parse_cmd.add_argument(
        "--workdir", default=str(DEFAULT_DATA_DIR / "parsed"), help="MinerU 下载与解压目录"
    )
    parse_cmd.set_defaults(func=run_parse)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
