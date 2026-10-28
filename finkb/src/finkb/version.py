"""解析管线的版本指纹。

`data/parsed/<doc>/` 下大部分文件是**派生产物**（`page_anchored.md`、`tables/`、
`logical_tables/`、`report.json` 的统计段），由 `content_list.json` 经当前代码算出。
改了判据之后不重跑，磁盘上的产物就和代码不一致——而且**看不出来**：`audit` 的指标
是从 `content_list.json` 现算的，永远不会读这些 .md，所以「指标没变」不能证明产物是新的。

指纹取参与解析的源文件内容哈希，写进 `report.json`。代码一改指纹就变，`audit
invariants` 会报「产物陈旧」。这样不需要谁记得手动 bump 版本号。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

#: 参与解析判据的源文件（相对 ``src/finkb/``）。改这些文件就会改变指纹。
_SOURCES = (
    "common/models.py",
    "parser/markdown_blocks.py",
    "parser/mineru_client.py",
    "parser/noise_filter.py",
    "parser/section_splitter.py",
    "parser/table_stitcher.py",
)


def pipeline_fingerprint() -> str:
    """返回当前解析代码的内容指纹（12 位十六进制）。"""
    root = Path(__file__).resolve().parent
    digest = sha256()
    for relative in _SOURCES:
        digest.update(relative.encode("utf-8"))
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()[:12]
