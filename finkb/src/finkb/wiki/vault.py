"""wiki_vault 的目录、原子写入与索引维护（ADR-041 / ADR-043）。

写文件为什么这么麻烦：L2 的产物要能进 git、要能被 lint、还要在机器突然断电
或进程被 kill 之后仍然可读。所以每次写入都是「同目录临时文件 → fsync → 原子
rename → fsync 父目录」；写到一半的页面永远不会出现在正式目录里。

锁用标准库 ``fcntl``（POSIX 文件锁），不引入 portalocker：少一个依赖，离线
测试也不用装包。锁文件固定为 ``<vault>/.wiki.lock``。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from finkb.wiki.frontmatter import (
    FrontmatterError,
    dump_frontmatter,
    split_frontmatter,
    validate,
)

FINKB_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VAULT = FINKB_ROOT / "wiki_vault"
DEFAULT_SCOPE = FINKB_ROOT / "config" / "wiki_scope.yaml"

TYPE_DIRS = {
    "summary": "summaries",
    "concept": "concepts",
    "decision": "concepts",
    "risk": "concepts",
    "entity": "entities",
    "exploration": "explorations",
}

INDEX_SECTIONS = (
    ("summaries", "Summaries（每份文档一份）"),
    ("concepts", "Concepts（跨文档对比 / 决策 / 风险）"),
    ("entities", "Entities（公司等实体）"),
    ("explorations", "Explorations（问答与探索）"),
)

INDEX_HEADER = (
    "# FinKB 知识库索引\n\n"
    "> 本文件由 `python -m finkb.wiki index` 生成，请勿手改。\n"
    "> 只列 `status: active` 的页面；`_draft/` 不进索引。\n"
)


@dataclass(frozen=True)
class Page:
    """一份已落盘的页面。"""

    path: Path
    meta: dict
    body: str

    @property
    def rel(self) -> str:
        return self.path.as_posix()


@contextlib.contextmanager
def vault_lock(root: Path):
    """对整个 vault 加排他锁，避免两个进程同时改 index.md。"""
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".wiki.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write(path: Path, text: str) -> None:
    """原子写：临时文件 → fsync → replace → fsync 父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def init_vault(root: Path | str = DEFAULT_VAULT) -> Path:
    """建目录骨架与空索引；已存在则不覆盖。"""
    root = Path(root)
    for name in ("summaries", "concepts", "entities", "sources", "explorations", "reports"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in ("summaries", "concepts", "entities", "explorations"):
        (root / "_draft" / name).mkdir(parents=True, exist_ok=True)
    if not (root / "index.md").exists():
        atomic_write(root / "index.md", INDEX_HEADER + "\n")
    if not (root / "log.md").exists():
        atomic_write(root / "log.md", "# FinKB 变更日志\n\n")
    return root


def page_path(root: Path, page_type: str, slug: str, *, status: str = "active") -> Path:
    """按类型给出页面路径；draft 统一进 ``_draft/<类型目录>/``。"""
    directory = TYPE_DIRS.get(page_type)
    if directory is None:
        raise FrontmatterError(f"未知页面类型: {page_type}")
    base = root / ("_draft" if status == "draft" else "")
    return base / directory / f"{slug}.md"


def write_page(
    root: Path,
    page_type: str,
    slug: str,
    meta: dict,
    body: str,
    *,
    status: str | None = None,
) -> Path:
    """校验并原子写一页；``status`` 覆盖 meta 里的状态（用于 draft → active）。"""
    meta = dict(meta)
    if status is not None:
        meta["status"] = status
    problems = validate(meta)
    if problems:
        raise FrontmatterError("; ".join(problems))
    if re.search(r"^---\s*$", body, re.MULTILINE):
        raise FrontmatterError("正文不得出现单独一行 '---'（会被解析成第二个 frontmatter）")
    path = page_path(root, page_type, slug, status=meta["status"])
    text = dump_frontmatter(meta) + "\n" + body.lstrip("\n").rstrip("\n") + "\n"
    atomic_write(path, text)
    return path


def read_page(path: Path) -> Page:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    return Page(path=path, meta=meta, body=body)


def iter_pages(root: Path, *, include_draft: bool = False) -> list[Page]:
    """按固定顺序读全部页面。"""
    pages: list[Page] = []
    for directory, _ in INDEX_SECTIONS:
        base = root / directory
        if base.is_dir():
            for path in sorted(base.glob("*.md")):
                pages.append(read_page(path))
    if include_draft:
        for directory, _ in INDEX_SECTIONS:
            base = root / "_draft" / directory
            if base.is_dir():
                for path in sorted(base.glob("*.md")):
                    pages.append(read_page(path))
    return pages


def render_index(pages: list[Page]) -> str:
    """生成 index.md 文本（确定性：只依赖页面路径与元数据）。"""
    lines = [INDEX_HEADER]
    active = [p for p in pages if p.meta.get("status") == "active"]
    for directory, title in INDEX_SECTIONS:
        group = [p for p in active if p.path.parent.name == directory]
        lines.append(f"## {title}\n")
        if not group:
            lines.append("（暂无）\n")
            continue
        for page in sorted(group, key=lambda p: p.path.name):
            stem = page.path.stem
            label = str(page.meta.get("title") or stem)
            description = str(page.meta.get("description") or "").strip()
            lines.append(f"- [[{directory}/{stem}|{label}]] — {description}\n")
        lines.append("")
    return "".join(lines)


def rebuild_index(root: Path) -> str:
    """重算并写入 index.md，返回文本。"""
    pages = iter_pages(root)
    text = render_index(pages)
    with vault_lock(root):
        atomic_write(root / "index.md", text)
    return text


def append_log(root: Path, event: str, detail: str = "") -> None:
    """往 log.md 追加一行（人读的时间轴；git 是机器时间轴）。"""
    stamp = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"- {stamp} {event}"
    if detail:
        line += f" — {detail}"
    path = root / "log.md"
    with vault_lock(root):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


# ---- 文档范围（ADR-042） ----------------------------------------------------


def load_scope(path: Path | str = DEFAULT_SCOPE) -> list[dict]:
    """读 ``wiki_scope.yaml``；文件不存在时返回空列表。"""
    path = Path(path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    docs = data.get("docs") or []
    if not isinstance(docs, list):
        raise ValueError("wiki_scope.yaml 的 docs 必须是列表")
    return docs


def registered_doc_ids(path: Path | str = DEFAULT_SCOPE) -> list[str]:
    return [str(doc.get("doc_id")) for doc in load_scope(path) if doc.get("doc_id")]
