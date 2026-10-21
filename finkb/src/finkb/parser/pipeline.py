"""Phase 0 端到端流水线：页码锚定 Markdown → 逻辑表集合。

串起四个环节，调用方只需一个函数::

    pages = parse_page_anchored_markdown(markdown, doc_id)
    report = strip_running_noise(pages)      # 剥离运行页眉/页脚
    result = stitch_tables(pages)            # 跨页缝合

顺序是强制的：**先清洗、后缝合**。缝合的判据依赖「页首/页尾是什么块」，
未清洗的页眉会让判据失效。两者叠加比单独任一都稳：清洗尽可能删掉噪音，
缝合再通过 ``is_noise`` 跳过漏网的噪音块。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from finkb.common.models import Page, StitchResult
from finkb.parser.markdown_blocks import parse_page_anchored_markdown
from finkb.parser.noise_filter import NoiseConfig, NoiseReport, strip_running_noise
from finkb.parser.table_stitcher import StitchOptions, stitch_tables


@dataclass
class Phase0Result:
    """Phase 0 的完整产物。"""

    pages: list[Page]
    noise: NoiseReport
    stitch: StitchResult

    @property
    def tables(self):
        return self.stitch.tables

    @property
    def stitched_count(self) -> int:
        return self.stitch.stitched_count

    def summary(self) -> str:
        return (
            f"页数={len(self.pages)} "
            f"逻辑表={len(self.tables)} "
            f"跨页缝合={self.stitched_count} "
            f"剥离噪音行={self.noise.removed_lines}"
        )


def build_tables(
    markdown: str,
    doc_id: str,
    noise_config: NoiseConfig | None = None,
    stitch_options: StitchOptions | None = None,
) -> Phase0Result:
    """执行「解析 → 清洗 → 缝合」全流程。"""
    pages = parse_page_anchored_markdown(markdown, default_doc=doc_id)
    noise_report = strip_running_noise(pages, noise_config)
    stitch_result = stitch_tables(pages, stitch_options)
    return Phase0Result(pages=pages, noise=noise_report, stitch=stitch_result)


def build_tables_from_file(
    path: str | Path,
    doc_id: str | None = None,
    noise_config: NoiseConfig | None = None,
    stitch_options: StitchOptions | None = None,
) -> Phase0Result:
    """从页码锚定 Markdown 文件执行全流程。``doc_id`` 缺省取文件名主干。"""
    file_path = Path(path)
    resolved_doc = doc_id or file_path.stem
    return build_tables(
        file_path.read_text(encoding="utf-8"),
        doc_id=resolved_doc,
        noise_config=noise_config,
        stitch_options=stitch_options,
    )
