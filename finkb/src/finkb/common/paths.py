"""统一路径入口：把「代码」与「用户本地知识库」分开。

``FINKB_HOME`` 指向用户自己的知识库目录（数据、配置、原文、索引、wiki 都在里面）。
不设置时：源码仓库里运行就用仓库根目录（``finkb/``），pip 安装后默认 ``~/.finkb``。
这样分发出去的代码不带任何语料，用户导入的年报只留在本机。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[3]  # finkb/（源码树）
_DEFAULT_HOME = (
    PACKAGE_ROOT
    if (PACKAGE_ROOT / "config" / "metric_dict.yaml").exists()
    else Path.home() / ".finkb"
)
HOME = Path(os.environ.get("FINKB_HOME") or _DEFAULT_HOME).expanduser().resolve()

# 用户库里需要有一份可改的配置模板；只复制缺失项，不覆盖用户改动。
CONFIG_GLOBS = ("*.yaml", "*.yml")


def home() -> Path:
    return HOME


def is_custom_home() -> bool:
    return HOME != PACKAGE_ROOT


def config_dir() -> Path:
    return HOME / "config"


def data_dir() -> Path:
    return HOME / "data"


def raw_dir() -> Path:
    """原文 PDF 与 manifest.csv。源码树里 raw/ 在仓库根（finkb 的上一级）。"""
    if not is_custom_home():
        return PACKAGE_ROOT.parent / "raw"
    return HOME / "raw"


def parsed_dir() -> Path:
    return data_dir() / "parsed"


def search_dir() -> Path:
    return data_dir() / "_search"


def wiki_vault_dir() -> Path:
    return HOME / "wiki_vault"


def facts_db() -> Path:
    return data_dir() / "facts.duckdb"


def wiki_db() -> Path:
    return data_dir() / "wiki.duckdb"


def chunks_path() -> Path:
    return search_dir() / "chunks.jsonl"


def chunks_db() -> Path:
    return search_dir() / "chunks.db"


def vectors_path() -> Path:
    return search_dir() / "vectors.npy"


def audit_log_path() -> Path:
    return data_dir() / "qa_audit.jsonl"


def package_config_dir() -> Path:
    """找到随包安装的配置模板目录（源码树 / wheel 的 share/finkb/config）。"""
    candidates = (
        PACKAGE_ROOT / "config",
        Path(sys.prefix) / "share" / "finkb" / "config",
        Path(__file__).resolve().parent.parent / "config",
    )
    for candidate in candidates:
        if (candidate / "metric_dict.yaml").exists():
            return candidate
    return candidates[0]


def env_file() -> Path:
    """优先用户库里的 .env；没有就回退包内的 .env（方便本机开发）。"""
    local = config_dir() / ".env"
    if local.exists():
        return local
    return package_config_dir() / ".env"


def ensure_home() -> Path:
    """建目录、复制配置模板、初始化空的 wiki_scope；幂等。"""
    for directory in (
        config_dir(),
        data_dir(),
        parsed_dir(),
        search_dir(),
        wiki_vault_dir(),
        raw_dir(),
    ):
        directory.mkdir(parents=True, exist_ok=True)
    source = package_config_dir()
    if source.exists():
        for pattern in CONFIG_GLOBS:
            for src in source.glob(pattern):
                # wiki_scope 是「这个库导入了哪些文档」的清单，不是可复制的模板。
                if src.name == "wiki_scope.yaml":
                    continue
                dst = config_dir() / src.name
                if not dst.exists():
                    shutil.copy2(src, dst)
        # 密钥模板单独复制（.env 本身永远不复制，也不进 git）。
        env_example = source / ".env.example"
        if env_example.exists() and not (config_dir() / ".env.example").exists():
            shutil.copy2(env_example, config_dir() / ".env.example")
    scope = config_dir() / "wiki_scope.yaml"
    if not scope.exists():
        scope.write_text("version: 1\ndocs: []\n", encoding="utf-8")
    return HOME
