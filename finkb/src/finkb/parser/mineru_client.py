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
    """把 ``Page``/``Block`` 渲染成带 META 注释的 Markdown。"""
    chunks: list[str] = []
    for page in pages:
        for block in page.blocks:
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

    MinerU 的 ``page_idx`` 是 **0-based**，这里统一转成 1-based 物理页码，
    与方案 §9.3 的锚点约定一致。

    章节标题会被解析为 ``heading`` 块，并作为后续块的 ``section`` 继承下去，
    这样 L1 抽取时能拿到「第三节 管理层讨论与分析」这类上下文。
    """
    pages: dict[int, Page] = {}
    current_section: str | None = None

    for item in content_list:
        page_no = int(item.get("page_idx", 0)) + 1
        item_type = str(item.get("type", "text"))
        text = _item_text(item)
        if not text.strip():
            continue

        if _is_heading(item):
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
            table = _table_block_from_item(item, meta, block_index)
            if table is not None:
                # 表格块保留 Markdown 文本形态，便于落盘与人工核对
                text = html_table_to_markdown(
                    str(item.get("table_body") or item.get("html") or "")
                ) or text

        page.blocks.append(
            Block(
                meta=meta,
                kind=kind,  # type: ignore[arg-type]
                lines=text.strip().splitlines(),
                block_index=block_index,
                table=table,
            )
        )

    return [pages[key] for key in sorted(pages)]


def _table_block_from_item(
    item: dict[str, Any],
    meta: DocMeta,
    block_index: int,
) -> TableBlock | None:
    """从 MinerU 表格条目直接构造 :class:`TableBlock`。

    不经过 Markdown 往返，因为 Markdown 管道表在语法上**无法表达「无表头」**，
    而跨页续接表恰恰就是无表头的。往返一趟会把首行数据行误当成表头，导致缝合
    误判「带独立表头 → 新表」。
    """
    body = item.get("table_body") or item.get("html")
    if not isinstance(body, str) or not body.strip():
        return None

    rows, first_is_header = html_table_to_rows(body)
    if not rows:
        return None

    header = rows[0] if first_is_header else None
    data_rows = rows[1:] if first_is_header else rows
    return TableBlock(
        meta=meta,
        header=list(header) if header is not None else None,
        rows=[list(row) for row in data_rows],
        has_separator=first_is_header,
        block_index=block_index,
        raw_lines=[],
    )


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
