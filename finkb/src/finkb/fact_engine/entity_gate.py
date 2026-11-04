"""实体名门禁：判断表格行标签是不是「实体名」（ADR-059）。

关系抽取的主要噪音不是表头认错，而是把「合计」「占净资产比例」「其中：」
这类行标签当公司名写进边表。这里先用一张可配置词表 + 形状规则挡掉，
挡不掉的再进注册表；挡错只影响召回，不会污染图。
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

from finkb.fact_engine.selector import FINKB_ROOT

GATE_PATH = FINKB_ROOT / "config" / "entity_gate.yaml"

# 行首序号：「1.」「（1）」「①」「1、」等，公司名不会这么写。
_NUM_PREFIX = re.compile(r"^[\s（(]*[0-9]+[\s、.．)）]*")
_CIRCLED = re.compile(r"^[\s]*[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳][\s、.]*")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_LETTER = re.compile(r"[A-Za-z]")
_NUMERIC_ONLY = re.compile(r"^[\d.,%\-—()（）\s]+$")
_PAREN = re.compile(r"[（(][^（()）]*[)）]")

# 公司名后缀：做别名匹配时去掉，避免「XX有限公司」和「XX」对不上。
_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "集团有限公司",
    "有限公司",
    "集团公司",
    "集团",
    "股份",
    "控股",
    "公司",
)


@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    file = Path(path)
    if not file.exists():
        return {"skip_exact": [], "skip_tokens": [], "min_length": 2, "max_length": 60}
    return yaml.safe_load(file.read_text(encoding="utf-8")) or {}


def load_config(path: str | Path | None = None) -> dict:
    """读门禁词表；路径参数便于测试替换。"""
    return _load(str(path or GATE_PATH))


def normalize(name: object) -> str:
    """归一实体名：全角转半角、去行首序号、去空白、去首尾标点。"""
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = text.strip()
    text = _CIRCLED.sub("", text)
    text = _NUM_PREFIX.sub("", text)
    text = re.sub(r"\s+", "", text)
    return text.strip("：:;；,，。.、|/-—_")


def gate_reason(name: object, cfg: dict | None = None) -> str | None:
    """判为噪音时返回原因，判为实体名时返回 None。"""
    cfg = cfg or load_config()
    text = normalize(name)
    if len(text) < int(cfg.get("min_length", 2)):
        return "too_short"
    if len(text) > int(cfg.get("max_length", 60)):
        return "too_long"
    if text in set(cfg.get("skip_exact") or []):
        return "generic"
    for token in cfg.get("skip_tokens") or []:
        if token and token in text:
            return f"token:{token}"
    if not _CJK.search(text) and not _LETTER.search(text):
        return "no_text"
    if _NUMERIC_ONLY.match(text):
        return "numeric"
    return None


def is_entity_name(name: object, cfg: dict | None = None) -> bool:
    return gate_reason(name, cfg) is None


def match_keys(name: object) -> list[str]:
    """生成用于跨文档匹配的别名：全名、去括号、去公司后缀。"""
    text = normalize(name)
    if not text:
        return []
    keys = [text]
    no_paren = _PAREN.sub("", text).strip()
    if no_paren and no_paren not in keys:
        keys.append(no_paren)
    for key in list(keys):
        for suffix in _SUFFIXES:
            if key.endswith(suffix) and len(key) > len(suffix) + 1:
                short = key[: -len(suffix)]
                if short not in keys:
                    keys.append(short)
                break
    return keys
