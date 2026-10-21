"""页眉页脚等重复噪音的识别与剥离（缝合前的必要清洗）。

为什么必须有这一步
------------------
A 股年报每一页的页首都有同一行运行页眉，例如::

    比亚迪股份有限公司 2025 年年度报告全文          43

跨页缝合的判据之一是「第 N+1 页的**第一个块**是表格」。若 MinerU 把页眉
输出成一个独立文本块，那么该页第一个块就是页眉而不是表格，缝合直接失败。
在真实年报上，这会大幅拉低缝合命中率。

识别原理
--------
不依赖固定文案，而是用**跨页重复**这一统计特征：

1. 取每页首块的头部若干行、末块的尾部若干行作为候选；
2. 把候选行里的数字一律替换为 ``#``（页眉的页码在变，文字骨架不变），
   得到「签名」；
3. 某签名在超过阈值比例的页面上出现 → 判定为运行页眉/页脚。

纯数字行（如单独的页码 ``43``）签名退化为 ``#``，信息量太低，会被
``min_signature_chars`` 直接排除，避免误删正文里的孤立数字。

处理方式
--------
**不删除块，只删噪音行并打标**：块内噪音行被剔除；若某块因此变空，则置
``is_noise=True`` 保留占位。这样缝合阶段可以跳过它、审计阶段可以追溯它。
需要物理移除时调用 :func:`drop_noise_blocks`。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from finkb.common.models import Page

_DIGITS_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")
_PAGE_NUMBER_RE = re.compile(r"^[\s\-—–()（）]*(\d{1,4})[\s\-—–()（）]*$")


@dataclass
class NoiseConfig:
    """噪音识别参数。

    Attributes:
        min_pages: 少于此页数不做检测——证据不足，宁可不删。
        min_ratio: 签名需出现在多少比例的页面上才判定为运行页眉/页脚。
        scan_head_lines: 每页首块参与检测的头部行数。
        scan_tail_lines: 每页末块参与检测的尾部行数。
        min_signature_chars: 签名中非数字字符的最少个数。用于排除纯页码行。
        max_signature_chars: 签名最大长度。用于排除长正文行被误判。
    """

    min_pages: int = 3
    min_ratio: float = 0.30
    scan_head_lines: int = 2
    scan_tail_lines: int = 2
    min_signature_chars: int = 4
    max_signature_chars: int = 80


@dataclass
class NoiseReport:
    """清洗结果，供审计与质量报表使用。"""

    signatures: set[str] = field(default_factory=set)
    removed_lines: int = 0
    affected_pages: list[int] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.removed_lines > 0


def line_signature(text: str, config: NoiseConfig | None = None) -> str | None:
    """计算一行文本的「去数字签名」；不适合做候选时返回 ``None``。"""
    cfg = config or NoiseConfig()
    stripped = text.strip()
    if not stripped:
        return None
    # 表格行永不参与噪音判定：跨页表头会在每页重复出现，一旦被当成页眉清掉，
    # 缝合后就没有列名可用了。这是本模块最重要的一条护栏。
    if stripped.startswith("|"):
        return None

    signature = _DIGITS_RE.sub("#", stripped)
    signature = _WS_RE.sub("", signature)

    meaningful = sum(1 for char in signature if char != "#")
    if meaningful < cfg.min_signature_chars:
        return None
    if len(signature) > cfg.max_signature_chars:
        return None
    return signature


def _candidate_lines(page: Page, config: NoiseConfig) -> list[str]:
    """取一页中参与检测的候选行（页首若行、页尾若行）。

    只考察**非表格块**：表格块的首行是表头，末行是数据，都不属于页眉页脚。

    当整页只有一个文本块时，头部行与尾部行取自同一块，必须去重——否则同一行
    会被计两次，阈值被虚高，普通正文会被误判为页眉。
    """
    text_blocks = [
        block
        for block in page.blocks
        if not block.is_noise and block.kind != "table"
    ]
    if not text_blocks:
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    for line in (
        text_blocks[0].lines[: config.scan_head_lines]
        + text_blocks[-1].lines[-config.scan_tail_lines :]
    ):
        if line in seen:
            continue
        seen.add(line)
        candidates.append(line)
    return candidates


def detect_signatures(
    pages: Sequence[Page],
    config: NoiseConfig | None = None,
) -> Counter[str]:
    """统计候选行的签名频次。"""
    cfg = config or NoiseConfig()
    counter: Counter[str] = Counter()
    for page in pages:
        for line in _candidate_lines(page, cfg):
            signature = line_signature(line, cfg)
            if signature:
                counter[signature] += 1
    return counter


def _threshold(total_pages: int, config: NoiseConfig) -> int:
    """判定阈值：比例阈值与硬下限 2 取大。

    硬下限不可省——比例阈值在小文档上会退化为 1，导致「只出现一次」的普通
    正文行也被判为噪音。运行页眉必然跨页重复，出现两次以上才有统计意义。
    """
    return max(2, math.ceil(total_pages * config.min_ratio))


def is_page_number_line(line: str, page_number: int) -> bool:
    """判断一行是否「就是本页页码」。

    只在该页文本块的头/尾极小范围内启用，且要求数字等于物理页码，误删风险很低。
    存在的理由：页眉常是 ``公司名 年份 报告全文  44``，签名匹配只能删掉文字部分，
    页码 ``44`` 会作为独立行残留，进而让缝合误以为「页首第一个块是文本」。
    """
    match = _PAGE_NUMBER_RE.match(line)
    return bool(match) and int(match.group(1)) == page_number


def _edge_block_positions(page: Page) -> tuple[int | None, int | None]:
    """返回该页首个、末个文本块的位置。"""
    positions = [
        index
        for index, block in enumerate(page.blocks)
        if not block.is_noise and block.kind != "table"
    ]
    if not positions:
        return None, None
    return positions[0], positions[-1]


def strip_running_noise(
    pages: Sequence[Page],
    config: NoiseConfig | None = None,
) -> NoiseReport:
    """就地剥离运行页眉/页脚与残留页码行，返回清洗报告。

    幂等：对已清洗过的结果再调用一次不会产生额外删除。
    """
    cfg = config or NoiseConfig()
    report = NoiseReport()

    if len(pages) < cfg.min_pages:
        return report

    counter = detect_signatures(pages, cfg)
    threshold = _threshold(len(pages), cfg)
    noisy = {signature for signature, count in counter.items() if count >= threshold}

    report.signatures = noisy

    for page in pages:
        page_changed = False
        head_pos, tail_pos = _edge_block_positions(page)

        for position, block in enumerate(page.blocks):
            if block.is_noise or block.kind == "table":
                continue

            in_head_zone = position == head_pos
            in_tail_zone = position == tail_pos
            total = len(block.lines)
            # 整块只有一个非空行时，该块本身可能就是独立的页码块（MinerU 常见形态）。
            # 这种块常常既不满足「页首块」也不满足「页尾块」，必须单独放行判定。
            single_line_block = sum(1 for line in block.lines if line.strip()) == 1

            kept: list[str] = []
            for line_pos, line in enumerate(block.lines):
                signature = line_signature(line, cfg)
                removable = signature is not None and signature in noisy

                if not removable and (
                    in_head_zone or in_tail_zone or single_line_block
                ):
                    near_edge = (
                        single_line_block
                        or (in_head_zone and line_pos < cfg.scan_head_lines)
                        or (in_tail_zone and line_pos >= total - cfg.scan_tail_lines)
                    )
                    removable = near_edge and is_page_number_line(line, page.number)

                if removable:
                    report.removed_lines += 1
                    page_changed = True
                    continue
                kept.append(line)

            if not kept and block.lines:
                block.is_noise = True
                block.table = None
            block.lines = kept

        if page_changed:
            report.affected_pages.append(page.number)

    return report


def drop_noise_blocks(pages: Sequence[Page]) -> int:
    """物理移除已打标的噪音块，返回移除数量。

    注意：移除后块的 ``block_index`` 会与页内位置不再一致（它们保持原值），
    因此**缝合阶段请使用页内位置**而非 ``block_index`` 作为游标。
    """
    removed = 0
    for page in pages:
        kept = [block for block in page.blocks if not block.is_noise]
        removed += len(page.blocks) - len(kept)
        page.blocks = kept
    return removed
