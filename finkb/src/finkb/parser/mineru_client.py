"""MinerU 解析客户端与离线兜底解析器（L0 凭证层入口）。

职责
----
1. 调用 MinerU 在线 API 把 PDF 解析为 ``full.md`` + ``content_list.json``；
2. 把 ``content_list.json`` 转成 **页码锚定 Markdown**（方案 §7.1 的中间格式）；
3. 在无 token / 无网络时，用 ``pypdf`` 逐页抽取做**低保证真度兜底**，
   保证整条流水线在离线环境下仍可跑通与测试。

⚠️ 端点说明
-----------
MinerU 在线 API 的路径与参数**尚未在本项目中实测**（当前环境无 token）。
因此所有端点都做成可配置项，默认值按 MinerU v4 公共 API 的通行约定填写。
接入真实环境前请对照官方文档校正 ``MineruConfig`` 中的路径，不要硬编码。

兜底解析器的定位
----------------
``OfflinePdfParser`` 只做逐页纯文本抽取，**不做版面分析、不还原表格结构**，
因此产出不能直接用于 L1 数值抽取，仅用于：链路连通性验证、页码对齐检查、
以及单元测试。生产路径必须使用 MinerU。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests

from finkb.common.models import Block, DocMeta, Page, TableBlock
from finkb.parser.markdown_blocks import (
    html_table_to_markdown,
    html_table_layout,
    html_table_to_rows,
    parse_page_anchored_markdown,
)

DEFAULT_BASE_URL = "https://mineru.net"
DEFAULT_SUBMIT_PATH = "/api/v4/extract/task"
DEFAULT_STATUS_PATH = "/api/v4/extract/task/{task_id}"
DEFAULT_UPLOAD_URLS_PATH = "/api/v4/file-urls/batch"
DEFAULT_BATCH_RESULT_PATH = "/api/v4/extract-results/batch/{batch_id}"


class MineruError(RuntimeError):
    """MinerU 调用失败。"""


@dataclass
class MineruConfig:
    """MinerU 客户端配置。所有路径均可覆盖，避免把未验证的端点写死。

    端点约定参照第三方实测规范（pipeline 2.6.4，见 README 引用）：
    本地文件走 ``file-urls/batch`` 预签名上传，URL 直传走 ``extract/task``。
    """

    token: str = ""
    base_url: str = DEFAULT_BASE_URL
    submit_path: str = DEFAULT_SUBMIT_PATH
    status_path: str = DEFAULT_STATUS_PATH
    upload_urls_path: str = DEFAULT_UPLOAD_URLS_PATH
    batch_result_path: str = DEFAULT_BATCH_RESULT_PATH
    poll_interval: float = 3.0
    poll_timeout: float = 900.0
    request_timeout: float = 60.0
    model_version: str = "pipeline"
    language: str = "ch"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MineruConfig":
        """从环境变量构造配置。

        变量名与默认值见 ``config/.env.example``。**不依赖 python-dotenv**：
        ``.env`` 文件由 :mod:`finkb.cli` 先行读入 ``os.environ``，此处只读环境变量，
        这样构造函数保持纯粹、可测试。

        Raises:
            MineruError: 未配置 token。缺 token 时应当立即失败，而不是带着空 token
                去发请求——那会得到难以理解的 401。
        """
        source: Mapping[str, str] = os.environ if env is None else env
        token = (source.get("MINERU_API_TOKEN") or source.get("MINERU_TOKEN") or "").strip()
        if not token:
            raise MineruError(
                "未找到 MINERU_API_TOKEN。请在 finkb/config/.env 中配置，"
                "模板见 finkb/config/.env.example"
            )

        def _num(key: str, default: float) -> float:
            raw = (source.get(key) or "").strip()
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        return cls(
            token=token,
            base_url=(source.get("MINERU_BASE_URL") or DEFAULT_BASE_URL).strip(),
            model_version=(source.get("MINERU_MODEL_VERSION") or "pipeline").strip(),
            language=(source.get("MINERU_LANGUAGE") or "ch").strip(),
            poll_interval=_num("MINERU_POLL_INTERVAL", cls.poll_interval),
            poll_timeout=_num("MINERU_POLL_TIMEOUT", cls.poll_timeout),
        )


@dataclass
class ParsedDocument:
    """一次解析的完整产物。"""

    doc_id: str
    pages: list[Page]
    markdown: str
    content_list: list[dict[str, Any]] = field(default_factory=list)
    source: str = "mineru"

    def to_page_anchored_markdown(self) -> str:
        """把页面 IR 渲染回页码锚定 Markdown（L0 的落盘格式）。"""
        return render_page_anchored_markdown(self.pages)


def render_page_anchored_markdown(pages: Iterable[Page]) -> str:
    """把 ``Page``/``Block`` 渲染成带 META 注释的 Markdown。

    已判 ``is_noise`` 的块**不渲染**：MinerU 的 ``header``/``footer`` 与
    统计出来的重复页眉都在这一类里。它们留着会让 ``page_anchored.md``
    每页多一行重复文字（比亚迪年报 267 行），而噪音本身的记录在
    ``NoiseReport`` 里，不需要正文再存一份。
    """
    chunks: list[str] = []
    for page in pages:
        for block in page.blocks:
            if block.is_noise:
                continue
            meta = {
                "doc": block.meta.doc_id,
                "page": block.meta.page,
            }
            if block.meta.section:
                meta["section"] = block.meta.section
            chunks.append(f"<!-- META: {json.dumps(meta, ensure_ascii=False)} -->")
            chunks.append(block.text)
            chunks.append("")
    return "\n".join(chunks).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# MinerU 在线客户端
# --------------------------------------------------------------------------- #
#: MinerU 单文件页数上限。超过会直接报错：
#: ``number of pages exceeds limit (200 pages), please split the file and try again``。
DEFAULT_PAGE_LIMIT = 200


class MineruClient:
    """MinerU 在线 API 的最小客户端。

    覆盖 Phase 0 需要的两条路径：
    - **本地文件上传**（主路径）：``request_upload_urls`` → ``upload_file``
      → ``wait_for_batch``；
    - **公网 URL 直传**：``submit_url`` → ``wait_for_result``。
    """

    def __init__(
        self,
        config: MineruConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config or MineruConfig()
        self.session = session or requests.Session()

    # -- 传输层：单独抽出，便于单测 monkeypatch，无需真实网络 --------------- #
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else f"{self.config.base_url}{path}"
        response = self.session.request(
            method,
            url,
            headers=self.config.headers,
            timeout=self.config.request_timeout,
            **kwargs,
        )
        if response.status_code >= 400:
            raise MineruError(f"{method} {url} 失败：HTTP {response.status_code} {response.text[:200]}")
        return response.json()

    # -- 公开 API ----------------------------------------------------------- #
    def submit_url(self, file_url: str, **extra: Any) -> str:
        """按文件 URL 提交解析任务，返回 ``task_id``。"""
        payload = {
            "url": file_url,
            "model_version": self.config.model_version,
            **extra,
        }
        data = self._request("POST", self.config.submit_path, json=payload)
        task_id = (data.get("data") or {}).get("task_id") or data.get("task_id")
        if not task_id:
            raise MineruError(f"响应中缺少 task_id：{data}")
        return str(task_id)

    def get_status(self, task_id: str) -> dict[str, Any]:
        """查询任务状态，返回原始 ``data`` 字典。"""
        path = self.config.status_path.format(task_id=task_id)
        data = self._request("GET", path)
        return data.get("data") or data

    # -- 本地文件上传路径（主路径） ---------------------------------------- #
    def request_upload_urls(
        self,
        file_name: str,
        data_id: str = "",
        page_ranges: str | None = None,
        **extra: Any,
    ) -> tuple[str, str]:
        """申请预签名上传 URL，返回 ``(batch_id, upload_url)``。

        Args:
            page_ranges: 只解析指定页码，如 ``"43-44"`` / ``"2,4-6"``。
                用于**低成本验证**——不必为 268 页的年报烧掉全部额度。
        """
        file_entry: dict[str, Any] = {"name": file_name}
        if data_id:
            file_entry["data_id"] = data_id
        if page_ranges:
            file_entry["page_ranges"] = page_ranges

        payload = {
            "files": [file_entry],
            "model_version": self.config.model_version,
            "enable_table": True,
            "enable_formula": True,
            "language": self.config.language,
            **extra,
        }
        data = self._request("POST", self.config.upload_urls_path, json=payload)
        body = data.get("data") or {}
        batch_id = body.get("batch_id")
        file_urls = body.get("file_urls") or []
        if not batch_id or not file_urls:
            raise MineruError(f"响应中缺少 batch_id 或 file_urls：{data}")
        return str(batch_id), str(file_urls[0])

    def upload_file(self, upload_url: str, pdf_path: Path) -> None:
        """裸 PUT 上传文件二进制。

        **不能携带任何请求头**（含 ``Content-Type`` / ``Authorization``），
        否则 OSS 预签名校验失败，返回 403 SignatureDoesNotMatch。
        """
        with open(pdf_path, "rb") as handle:
            response = self.session.put(
                upload_url,
                data=handle,
                timeout=self.config.poll_timeout,
            )
        if response.status_code >= 400:
            raise MineruError(
                f"上传失败：HTTP {response.status_code} {response.text[:200]}"
            )

    def wait_for_batch(self, batch_id: str) -> str:
        """轮询批量结果直到完成，返回结果 zip 的下载地址。"""
        path = self.config.batch_result_path.format(batch_id=batch_id)
        deadline = time.monotonic() + self.config.poll_timeout
        while True:
            data = self._request("GET", path)
            results = (data.get("data") or {}).get("extract_result") or []
            if results:
                state = str(results[0].get("state", "")).lower()
                if state in {"done", "success", "finished"}:
                    zip_url = results[0].get("full_zip_url")
                    if not zip_url:
                        raise MineruError(f"任务完成但缺少下载地址：{results[0]}")
                    return str(zip_url)
                if state in {"failed", "error"}:
                    raise MineruError(
                        f"任务失败：{results[0].get('err_msg') or results[0]}"
                    )
            if time.monotonic() > deadline:
                raise MineruError(f"批次 {batch_id} 轮询超时")
            time.sleep(self.config.poll_interval)

    def wait_for_result(self, task_id: str) -> str:
        """轮询直到任务完成，返回结果 zip 的下载地址。"""
        deadline = time.monotonic() + self.config.poll_timeout
        while True:
            status = self.get_status(task_id)
            state = str(status.get("state", "")).lower()
            if state in {"done", "success", "finished"}:
                zip_url = status.get("full_zip_url") or status.get("zip_url")
                if not zip_url:
                    raise MineruError(f"任务完成但缺少下载地址：{status}")
                return str(zip_url)
            if state in {"failed", "error"}:
                raise MineruError(f"任务失败：{status.get('err_msg') or status}")
            if time.monotonic() > deadline:
                raise MineruError(f"任务 {task_id} 轮询超时")
            time.sleep(self.config.poll_interval)

    def download_zip(self, zip_url: str, dest: Path) -> Path:
        """下载结果 zip 并解压到 ``dest``。"""
        dest.mkdir(parents=True, exist_ok=True)
        zip_path = dest / "result.zip"
        response = self.session.get(zip_url, timeout=self.config.request_timeout)
        if response.status_code >= 400:
            raise MineruError(f"下载失败：HTTP {response.status_code}")
        zip_path.write_bytes(response.content)
        extract_dir = dest / "extracted"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        with zipfile.ZipFile(zip_path) as archive:
            _safe_extract(archive, extract_dir)
        return extract_dir

    # -- 解析结果 ----------------------------------------------------------- #
    def parse_extracted(self, extract_dir: Path, doc_id: str) -> ParsedDocument:
        """把解压后的目录解析为 :class:`ParsedDocument`。"""
        md_files = sorted(extract_dir.rglob("*.md"))
        json_files = sorted(extract_dir.rglob("*content_list.json"))
        if not md_files:
            raise MineruError(f"{extract_dir} 下未找到 Markdown 输出")
        markdown = md_files[0].read_text(encoding="utf-8")

        content_list: list[dict[str, Any]] = []
        if json_files:
            content_list = json.loads(json_files[0].read_text(encoding="utf-8"))

        pages = (
            content_list_to_pages(content_list, doc_id)
            if content_list
            else parse_page_anchored_markdown(markdown, default_doc=doc_id)
        )
        return ParsedDocument(
            doc_id=doc_id,
            pages=pages,
            markdown=markdown,
            content_list=content_list,
            source="mineru",
        )

    def parse_pdf(
        self,
        pdf_path: Path,
        doc_id: str,
        workdir: Path,
        page_ranges: str | None = None,
    ) -> ParsedDocument:
        """端到端（本地文件）：申请预签名 URL → 裸 PUT 上传 → 轮询 → 下载 → 解析。"""
        batch_id, upload_url = self.request_upload_urls(
            pdf_path.name, data_id=doc_id, page_ranges=page_ranges
        )
        self.upload_file(upload_url, pdf_path)
        zip_url = self.wait_for_batch(batch_id)
        extract_dir = self.download_zip(zip_url, workdir / doc_id)
        return self.parse_extracted(extract_dir, doc_id)

    def parse_pdf_split(
        self,
        pdf_path: Path,
        doc_id: str,
        workdir: Path,
        page_limit: int = DEFAULT_PAGE_LIMIT,
    ) -> ParsedDocument:
        """超过 MinerU 单文件页数上限时自动分批解析并合并。

        超限时 MinerU 直接报错 ``number of pages exceeds limit``。这里按上限切页、
        逐批提交，再把各批 ``page_idx`` 平移后拼成一份 ``content_list``——
        **只有一份**，所以页码锚点、跨页缝合、`page_end` 回填都在同一套坐标系里。
        """
        ranges = chunk_ranges(pdf_page_count(pdf_path), page_limit)
        if len(ranges) <= 1:
            return self.parse_pdf(pdf_path, doc_id, workdir)

        batches: list[tuple[int, list[dict[str, Any]]]] = []
        for start, end in ranges:
            part = self.parse_pdf(
                pdf_path, doc_id, workdir, page_ranges=f"{start}-{end}"
            )
            batches.append((start, list(part.content_list)))
        merged = merge_batches(batches)
        return ParsedDocument(
            doc_id=doc_id,
            pages=content_list_to_pages(merged, doc_id),
            markdown="",
            content_list=merged,
        )

    def parse_url(self, file_url: str, doc_id: str, workdir: Path) -> ParsedDocument:
        """端到端（公网 URL）：submit → 轮询 → 下载 → 解析。"""
        zip_url = self.wait_for_result(self.submit_url(file_url))
        extract_dir = self.download_zip(zip_url, workdir / doc_id)
        return self.parse_extracted(extract_dir, doc_id)


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    """防 zip slip：拒绝解压到 target 之外的成员。"""
    target = target.resolve()
    for member in archive.namelist():
        destination = (target / member).resolve()
        if not str(destination).startswith(str(target)):
            raise MineruError(f"压缩包包含非法路径：{member}")
    archive.extractall(target)


# --------------------------------------------------------------------------- #
# content_list.json → Page IR
# --------------------------------------------------------------------------- #
_HEADING_TYPES = {"title", "heading"}

#: MinerU 显式标记的版面噪音类型。
#: 实测 pipeline 后端**不会过滤**页眉页脚，而是把它们作为独立块输出并打上类型标签
#: （VLM 后端还会额外产出 footer / aside_text / page_footnote 等）。
#: 有了类型标签就不必依赖统计推断——这是比 ``noise_filter`` 更可靠的信号。
_NOISE_TYPES = {"header", "footer", "page_number", "aside_text", "page_footnote"}

#: 带编号的章节标题前缀（``十三、`` / ``4、`` / ``（1）`` / ``第八节``）。
#: 表格前紧邻的这类块是章节标题，不是表头——P242 那种「表头被拆成标题块」的
#: 情形里，运行段的开头往往还挂着一个 ``4、其他关联方``。
_SECTION_NUMBER_RE = re.compile(
    r"^(?:[一二三四五六七八九十百]+[、.]|\d+[、.]"
    r"|[（(][一二三四五六七八九十\d]+[)）]|第[一二三四五六七八九十]+[节章])"
)

#: 表头单元格的长度上限。超过这个长度的标题属于章节名，不可能是列名。
_MAX_HEADER_CELL_CHARS = 12

#: 期间标签：整块文本只有一个年份，可选带「12 月 31 日」。
#: MinerU 常把它输出成表格前的一个独立文本块（实测比亚迪年报 39 处），
#: 于是同一页两张表头相同的表在数据层分不出年份（P167 的 T94/T95）。
_PERIOD_LABEL_RE = re.compile(
    r"^\s*(?:19|20)\d{2}\s*年\s*(?:12\s*月\s*31\s*日)?\s*$"
)

#: 表外标题的长度上限。超过这个长度的是正文段落，不是表名（实测赛力斯
#: P12/P13 表格上方就是成段的行业描述）。
_CAPTION_MAX_CHARS = 80

#: 表外标题的**弱**判据：冒号收尾之外，含这些词也大概率是表名。
#: 命中记 ``self``；不命中但仍短，照样收录，记 ``text``（低置信）。
_CAPTION_HINT_RE = re.compile(r"(如下|情况|明细|构成|列示|结构|安排)")

#: 单位 / 币种声明行：已经是表格的单位元数据，不是表名。
_UNIT_LINE_RE = re.compile(r"^(单位|币种|编制单位|金额单位)\s*[:：]")

#: 复选框行。MinerU 会给「√适用 □不适用」打 ``text_level``，但它既不是章节
#: 标题、也不是表名，两处都要排除。
_BOX_LINE_RE = re.compile(r"[□☑√✓]")
_BOX_LINE_MAX_CHARS = 15

#: 行尾粘着的复选框尾巴。有些表名和「□适用 √不适用」被 MinerU 粘成了同一行，
#: 去掉尾巴表名仍然可用（赛力斯 P72「前十名股东…发生变化□适用 √不适用」）。
_BOX_TAIL_RE = re.compile(r"[□☑√✓].*$")

#: 目录行（``第一节 释义....5``）。出现在表格上方是版面残留，不是表名。
_TOC_RE = re.compile(r"\.{4,}")


def _build_page_number_map(content_list: list[dict[str, Any]]) -> dict[int, int]:
    """建立 ``page_idx → 物理页码`` 映射。

    **为什么必须这么做**：使用 ``page_ranges`` 只解析部分页时，MinerU 返回的
    ``page_idx`` **会从 0 重新编号**，导致 ``page_idx + 1`` 得到的是「第 1 页」
    而不是真实物理页码。若不修正，所有 ``[Ref: doc#P1]`` 锚点都会指错页——
    这对「物理页码强溯源」是致命错误。

    MinerU 会为每页输出 ``type="page_number"`` 的块，其 ``text`` 就是**真实物理
    页码**，用它来修正。缺失该块时退回 ``page_idx + 1``。
    """
    mapping: dict[int, int] = {}
    for item in content_list:
        if str(item.get("type", "")) != "page_number":
            continue
        text = str(item.get("text") or "").strip()
        if not text.isdigit():
            continue
        mapping[int(item.get("page_idx", 0))] = int(text)
    return mapping


def pdf_page_count(pdf_path: Path) -> int:
    """数 PDF 页数；读不了返回 0（调用方据此退化为单次解析）。"""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf 是可选依赖
        return 0
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:  # pragma: no cover - 损坏文件不该让解析崩掉
        return 0


def chunk_ranges(total_pages: int, limit: int = DEFAULT_PAGE_LIMIT) -> list[tuple[int, int]]:
    """把 ``1..total_pages`` 按 ``limit`` 切成若干个 1 基闭区间。"""
    if total_pages <= 0 or limit <= 0:
        return []
    return [
        (start, min(start + limit - 1, total_pages))
        for start in range(1, total_pages + 1, limit)
    ]


def offset_content_list(items: list[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
    """把一批 ``content_list`` 的 ``page_idx`` 整体平移 ``offset``。

    MinerU 对每次请求都从 0 重新编号 ``page_idx``，两批直接拼会**互相覆盖页**，
    而且不会报错——这正是坑-16 的成因。``page_number`` 块的 ``text`` 本身是
    绝对页码（如 ``"202"``），不需要平移。
    """
    return [{**item, "page_idx": int(item.get("page_idx", 0)) + offset} for item in items]


def merge_batches(batches: Iterable[tuple[int, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """把多批解析结果按「本批起始物理页」拼成一份 ``content_list``。"""
    merged: list[dict[str, Any]] = []
    for start_page, items in batches:
        merged.extend(offset_content_list(items, start_page - 1))
    return merged


def _is_heading(item: dict[str, Any]) -> bool:
    """标题判定（按第三方实测规范修正）。

    content_list 中标题是 ``type="text"`` 且**携带 ``text_level`` 字段**；
    正文块的 ``text_level`` 字段**完全不存在**（不是 null / 0）。
    ``type="title"`` 是 layout.json 的写法，一并兼容。
    """
    item_type = str(item.get("type", "text"))
    if item_type in _HEADING_TYPES:
        return True
    return item_type == "text" and item.get("text_level") is not None


def content_list_to_pages(
    content_list: list[dict[str, Any]],
    doc_id: str,
) -> list[Page]:
    """把 MinerU 的 ``content_list.json`` 转成按页组织的 ``Page``。

    **物理页码优先取 ``page_number`` 块的真实值**，其次才退回 ``page_idx + 1``。
    原因见 :func:`_build_page_number_map`——分页解析时 ``page_idx`` 会从 0 重编号。

    页眉/页脚/页码块按 MinerU 的类型标签**直接标记为 ``is_noise=True``**，
    不需要统计推断。

    章节标题会被解析为 ``heading`` 块，并作为后续块的 ``section`` 继承下去，
    这样 L1 抽取时能拿到「第三节 管理层讨论与分析」这类上下文。
    """
    pages: dict[int, Page] = {}
    current_section: str | None = None
    page_map = _build_page_number_map(content_list)
    last_table: TableBlock | None = None
    tables_on_page: dict[int, int] = {}
    heading_run: list[str] = []
    prev_kind: str | None = None
    prev_text = ""
    prev_noise = False
    history: list[tuple[str, str, bool]] = []

    for item in content_list:
        raw_page_idx = int(item.get("page_idx", 0))
        page_no = page_map.get(raw_page_idx, raw_page_idx + 1)
        item_type = str(item.get("type", "text"))
        text = _item_text(item)
        if not text.strip():
            if item_type == "table":
                _extend_page_end(last_table, page_no, tables_on_page.get(page_no, 0))
            continue

        if item_type == "table":
            tables_on_page[page_no] = tables_on_page.get(page_no, 0) + 1

        if _is_heading(item) and not _is_box_line(text):
            current_section = text.strip()
            kind = "heading"
        elif item_type == "table":
            kind = "table"
        elif item_type in {"image", "figure"}:
            kind = "image"
        elif item_type in {"equation", "code"}:
            kind = "code"
        else:
            kind = "text"

        page = pages.setdefault(page_no, Page(number=page_no))
        meta = DocMeta(doc_id=doc_id, page=page_no, section=current_section)
        block_index = len(page.blocks)

        table = None
        if kind == "table":
            table = _table_block_from_item(item, meta, block_index, heading_run)
            if table is not None:
                last_table = table
                if prev_kind == "text" and not prev_noise and _PERIOD_LABEL_RE.match(prev_text):
                    # 期间标签是表格的元数据，不是正文里独立的一句话
                    table.period_label = prev_text.strip()
                table.caption, table.caption_source = _resolve_caption(history)
                # 表格块保留 Markdown 文本形态，便于落盘与人工核对
                text = html_table_to_markdown(
                    str(item.get("table_body") or item.get("html") or "")
                ) or text

        prev_kind, prev_text, prev_noise = kind, text.strip(), item_type in _NOISE_TYPES
        history.append((kind, text.strip(), prev_noise))

        if kind == "heading":
            # 只有**紧邻**表格的连续标题块才可能是被拆散的表头
            heading_run.append(text.strip())
        else:
            heading_run.clear()

        page.blocks.append(
            Block(
                meta=meta,
                kind=kind,  # type: ignore[arg-type]
                lines=text.strip().splitlines(),
                block_index=block_index,
                table=table,
                # MinerU 直接用类型标签告诉我们这是页眉/页脚/页码，
                # 不必等 noise_filter 去做统计推断。
                is_noise=item_type in _NOISE_TYPES,
            )
        )

    return [pages[key] for key in sorted(pages)]


def _is_box_line(text: str) -> bool:
    """复选框行（``√适用 □不适用``）。

    MinerU 会给这类行打 ``text_level``，于是它既会被当成章节标题继承进
    ``section``（实测两家 6 张表），又会在回溯表外标题时被误取。它两个都不是。
    """
    stripped = text.strip()
    return bool(_BOX_LINE_RE.search(stripped)) and len(stripped) <= _BOX_LINE_MAX_CHARS


def _resolve_caption(history: list[tuple[str, str, bool]]) -> tuple[str, str]:
    """从表格之前的内容块里回溯，找出这张表的**表外标题**。

    ``history`` 的元素是 ``(kind, text, is_noise)``，按出现顺序排列。

    Returns:
        ``(caption, caption_source)``；``caption_source`` 的取值见
        :attr:`finkb.common.models.TableBlock.caption_source`。
    """
    for kind, text, is_noise in reversed(history):
        if is_noise:
            continue
        if kind in {"table", "image", "code"}:
            return "", "adjacent_table"
        stripped = text.strip()
        if not stripped:
            continue
        if _PERIOD_LABEL_RE.match(stripped) or _UNIT_LINE_RE.match(stripped):
            continue  # 已经是表的元数据，不是表名
        if _TOC_RE.search(stripped):
            continue  # 目录行是版面残留
        if _is_box_line(stripped):
            continue
        stripped = _BOX_TAIL_RE.sub("", stripped).strip()
        if not stripped:
            continue
        if kind == "heading":
            return "", "heading"
        if len(stripped) > _CAPTION_MAX_CHARS:
            return "", "missing"
        if stripped.endswith(("：", ":")) or _CAPTION_HINT_RE.search(stripped):
            return stripped, "self"
        return stripped, "text"
    return "", "missing"


def _extend_page_end(
    last_table: TableBlock | None,
    page_no: int,
    tables_seen_on_page: int,
) -> None:
    """把空表格占位块的页码回填到上一张表。

    MinerU 遇到跨页表格时，会把所有行合并进起始页的那个块，并在后续页各留一个
    空的 table 块。空块没有内容，但它带着 ``page_idx``——这是「这张表续到了第几
    页」的唯一证据。丢掉它，表格的页码区间就永远是单页。

    三条护栏：

    - 必须已经有一张表，否则这个空块无主；
    - 必须是本页**第一个** table 块——出现在页中的空块属于它上面那张表；
    - 页码必须紧接上一张表的末页；断档说明中间另有内容，不能盲目接。
    """
    if last_table is None or tables_seen_on_page:
        return
    if page_no != last_table.last_page + 1:
        return
    last_table.page_end = page_no


def _table_block_from_item(
    item: dict[str, Any],
    meta: DocMeta,
    block_index: int,
    headings: list[str] | None = None,
) -> TableBlock | None:
    """从 MinerU 表格条目直接构造 :class:`TableBlock`。

    不经过 Markdown 往返，因为 Markdown 管道表在语法上**无法表达「无表头」**，
    而跨页续接表恰恰就是无表头的。往返一趟会把首行数据行误当成表头，导致缝合
    误判「带独立表头 → 新表」。

    ``headings`` 是紧邻表格的连续标题块。MinerU 偶尔把表头行拆成两个标题块
    （P242 的「公司名称 / 关联方关系」），此时 HTML 里根本没有表头——回收它。
    """
    body = item.get("table_body") or item.get("html")
    if not isinstance(body, str) or not body.strip():
        return None

    layout = html_table_layout(body)
    if not layout.rows and not layout.header:
        return None

    header: list[str] | None = list(layout.header) if layout.header else None
    header_rows = [list(row) for row in layout.header_rows]
    if header is None:
        rebuilt = _header_from_heading_run(headings, layout.width)
        if rebuilt is not None:
            header = rebuilt
            header_rows = [list(rebuilt)]

    return TableBlock(
        meta=meta,
        header=header,
        rows=[list(row) for row in layout.rows],
        has_separator=header is not None,
        block_index=block_index,
        raw_lines=[],
        section_rows=list(layout.section_rows),
        header_rows=header_rows,
        table_title=layout.table_title,
    )


def _header_from_heading_run(
    headings: list[str] | None,
    width: int,
) -> list[str] | None:
    """把紧邻表格的连续标题块当成被拆散的表头。

    三条约束都是结构事实，没有任何经验阈值：

    - 去掉开头带编号的章节标题（``4、其他关联方``），剩下的才可能是列名；
    - 剩下的格数必须**恰好等于列数**，多一个少一个都放弃；
    - 每格都得像个列名（长度有上限），长句子是正文不是列名。
    """
    if not headings or width < 2:
        return None
    cells = list(headings)
    while cells and _SECTION_NUMBER_RE.match(cells[0]):
        cells.pop(0)
    if len(cells) != width:
        return None
    if any(len(cell) > _MAX_HEADER_CELL_CHARS for cell in cells):
        return None
    return cells


def _item_text(item: dict[str, Any]) -> str:
    """从 content_list 条目中取出正文；表格优先用 HTML 并转成管道表。"""
    if item.get("type") == "table":
        body = item.get("table_body") or item.get("html")
        if body:
            converted = html_table_to_markdown(str(body))
            if converted:
                return converted
    for key in ("text", "content", "table_body"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


# --------------------------------------------------------------------------- #
# 离线兜底解析器
# --------------------------------------------------------------------------- #
class OfflinePdfParser:
    """基于 ``pypdf`` 的逐页文本兜底解析器。

    仅用于链路验证与测试。产出不含表格结构，**不可用于 L1 抽取**。
    """

    def __init__(self, doc_id: str) -> None:
        self.doc_id = doc_id

    def parse(self, pdf_path: Path) -> ParsedDocument:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        pages: list[Page] = []
        markdown_parts: list[str] = []

        for index, pdf_page in enumerate(reader.pages, start=1):
            text = (pdf_page.extract_text() or "").strip()
            if not text:
                continue
            meta = DocMeta(doc_id=self.doc_id, page=index, section=None)
            pages.append(
                Page(
                    number=index,
                    blocks=[
                        Block(
                            meta=meta,
                            kind="text",
                            lines=text.splitlines(),
                            block_index=0,
                        )
                    ],
                )
            )
            markdown_parts.append(text)

        return ParsedDocument(
            doc_id=self.doc_id,
            pages=pages,
            markdown="\n\n".join(markdown_parts),
            content_list=[],
            source="pypdf-fallback",
        )
