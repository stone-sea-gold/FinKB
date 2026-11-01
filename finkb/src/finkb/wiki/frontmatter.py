"""OKF frontmatter 的解析、校验与序列化（ADR-041 / ADR-043）。

为什么由代码拼 frontmatter：LLM 只负责正文（body_markdown），页面元数据必须
可校验、可复现。模型输出里一旦出现 ``---`` 就判违规重试——否则它会把分隔符
写进正文，把后面的内容变成「第二个 frontmatter」，解析器读到的字段就不可信了。
"""

from __future__ import annotations

import datetime as _dt
import re

import yaml

PAGE_TYPES = ("summary", "concept", "entity", "exploration", "decision", "risk")
STATUSES = ("draft", "active", "outdated")

# OKF 必填：type 与 description；其余是本项目自己加的约束。
REQUIRED_FIELDS = ("type", "title", "description", "status", "updated_at")

# ADR-041：来源按证据层计。l0: 是页码锚定原文，l1:facts# 是事实库。
SOURCE_PATTERN = re.compile(r"^(l0:.+|l1:(?:facts|segments)#.+)$")

# 固定字段顺序，保证同一页面重复生成时字节稳定（便于 git diff 与 lint 比对）。
FIELD_ORDER = (
    "type",
    "title",
    "description",
    "company",
    "period",
    "sources",
    "tags",
    "status",
    "updated_at",
)


class FrontmatterError(ValueError):
    """frontmatter 缺失或不合规。"""


def split_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 frontmatter 与正文；没有 frontmatter 时抛 ``FrontmatterError``。"""
    if not text.startswith("---\n"):
        raise FrontmatterError("文件必须以 '---' 开头")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise FrontmatterError("frontmatter 没有闭合的 '---'")
    raw = text[4:end]
    body = text[end + 5 :]
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - 防御性
        raise FrontmatterError(f"frontmatter 不是合法 YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise FrontmatterError("frontmatter 必须是映射")
    return meta, body


def dump_frontmatter(meta: dict) -> str:
    """按固定字段顺序输出 frontmatter（含首尾 ``---``）。"""
    ordered = {key: meta[key] for key in FIELD_ORDER if key in meta}
    for key, value in meta.items():
        if key not in ordered:
            ordered[key] = value
    text = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{text}---\n"


def source_layers(sources: object) -> set[str]:
    """返回来源里出现的证据层（``l0`` / ``l1``）。"""
    layers: set[str] = set()
    if not isinstance(sources, list):
        return layers
    for item in sources:
        text = str(item or "").strip()
        if text.startswith("l0:"):
            layers.add("l0")
        elif text.startswith(("l1:facts#", "l1:segments#")):
            layers.add("l1")
    return layers


def has_enough_sources(meta: dict) -> bool:
    """ADR-041：L0 与 L1 两层都在才算「来源 ≥2」。"""
    return source_layers(meta.get("sources")) >= {"l0", "l1"}


def validate(meta: dict) -> list[str]:
    """返回问题列表；空列表表示合规。"""
    problems: list[str] = []
    for key in REQUIRED_FIELDS:
        value = meta.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(f"缺少字段 {key}")
    page_type = meta.get("type")
    if page_type is not None and page_type not in PAGE_TYPES:
        problems.append(f"type 非法: {page_type}")
    status = meta.get("status")
    if status is not None and status not in STATUSES:
        problems.append(f"status 非法: {status}")
    description = meta.get("description")
    if isinstance(description, str) and len(description) > 200:
        problems.append("description 超过 200 字（索引只放摘要）")
    updated = meta.get("updated_at")
    if updated is not None and not _is_date(updated):
        problems.append(f"updated_at 不是日期: {updated}")
    sources = meta.get("sources")
    if sources is not None:
        if not isinstance(sources, list) or not sources:
            problems.append("sources 必须是非空列表")
        else:
            for item in sources:
                if not SOURCE_PATTERN.match(str(item or "").strip()):
                    problems.append(f"sources 条目格式非法: {item}（应为 l0:<doc> 或 l1:facts#<id> / l1:segments#<id>）")
    tags = meta.get("tags")
    if tags is not None and (not isinstance(tags, list) or any(not isinstance(t, str) for t in tags)):
        problems.append("tags 必须是字符串列表")
    return problems


def _is_date(value: object) -> bool:
    if isinstance(value, _dt.date):
        return True
    if not isinstance(value, str):
        return False
    try:
        _dt.date.fromisoformat(value[:10])
    except ValueError:
        return False
    return True


def build(
    *,
    page_type: str,
    title: str,
    description: str,
    sources: list[str],
    company: str | None = None,
    period: str | None = None,
    tags: list[str] | None = None,
    status: str = "active",
    updated_at: str | None = None,
) -> dict:
    """由代码拼一份合规 frontmatter。"""
    meta: dict = {
        "type": page_type,
        "title": title,
        "description": description,
        "sources": list(sources),
        "tags": list(tags or []),
        "status": status,
        "updated_at": updated_at or _dt.date.today().isoformat(),
    }
    if company:
        meta["company"] = company
    if period:
        meta["period"] = period
    problems = validate(meta)
    if problems:
        raise FrontmatterError("; ".join(problems))
    return meta
