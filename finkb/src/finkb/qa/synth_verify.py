"""合成答案校验（V1–V7）与失败矩阵（P3）。

设计见 ``docs/合成子系统设计.md`` §7。校验只保证：

- 数字来自账本、引用不造假、页码可回查、拒答说明缺什么；
- **不保证**语义解释一定正确（那要靠证据选择、提示词、judge 与人工抽检）。

P3 不接入 pipeline；``compose_verified`` 供 P4 调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from finkb.qa import verifier
from finkb.qa.evidence import EvidencePack
from finkb.qa.synth import (
    LLMBackend,
    SynthesisResult,
    build_prompt,
    normalize_response,
)
from finkb.qa.synthesizer import PLACEHOLDER_RE, render

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；\n])")
_DOC_ID_RE = re.compile(r"\b\d{6}_\d{4}_(?:年报|半年报|一季报|三季报)")
_CODE_RE = re.compile(r"\b\d{6}\b")
_PAGE_RE = re.compile(r"P\d+|第\s*\d+\s*页")
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_CONCLUSION_MIN_CHARS = 20


@dataclass
class VerificationResult:
    ok: bool = True
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": dict(self.checks),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "attempts": self.attempts,
        }


def _ledger_from_pack(pack: EvidencePack) -> list[dict[str, Any]]:
    return [
        {"id": fact.id, "value": fact.value, "unit": fact.unit, "format": fact.format}
        for fact in pack.facts
    ]


def _allowed_values(pack: EvidencePack) -> set[str]:
    allowed: set[str] = set()
    for fact in pack.facts:
        for value in (fact.value, fact.raw_value):
            if value is None:
                continue
            value = float(value)
            allowed.add(f"{value:.2f}")
            allowed.add(f"{int(round(value))}")
            allowed.add(f"{value:g}")
    return allowed


def _strip_noise(text: str) -> str:
    text = verifier.REF_RE.sub(" ", text)
    text = _DOC_ID_RE.sub(" ", text)
    text = _CODE_RE.sub(" ", text)
    text = _PAGE_RE.sub(" ", text)
    return text


def _numbers(text: str) -> list[str]:
    out: list[str] = []
    for raw in verifier.NUMBER_RE.findall(_strip_noise(text)):
        number = raw.replace(",", "")
        if _YEAR_RE.fullmatch(number):
            continue
        out.append(number)
    return out


def _refs(text: str) -> list[tuple[str, int]]:
    return [(doc, int(page)) for doc, page in verifier.REF_RE.findall(text)]


def _whitelist(pack: EvidencePack) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for citation in pack.citations:
        out.update(_refs(citation))
    for fact in pack.facts:
        if fact.doc:
            out.add((fact.doc, fact.page))
    for passage in pack.passages:
        out.add((passage.doc, passage.page))
    return out


def _cited_passage_text(pack: EvidencePack, refs: list[tuple[str, int]]) -> str:
    ref_set = set(refs)
    parts = [
        passage.text.replace(",", "")
        for passage in pack.passages
        if (passage.doc, passage.page) in ref_set
    ]
    return "\n".join(parts)


def _check_v1(result: SynthesisResult, pack: EvidencePack, checks: dict[str, bool], errors: list[str]) -> None:
    known = {fact.id for fact in pack.facts}
    # 只允许 cN（结构化事实）；模型偶尔会把检索片段写成 {{e1}}，必须拦下。
    unknown = sorted(
        {
            match.group(1).strip()
            for match in re.finditer(r"\{\{\s*([^}]+?)\s*\}\}", result.answer)
        }
        - known
    )
    checks["V1"] = not unknown
    if unknown:
        errors.append("占位符不在账本中：" + ", ".join(unknown))


def _check_v2(
    result: SynthesisResult,
    pack: EvidencePack,
    rendered: str,
    checks: dict[str, bool],
    errors: list[str],
) -> None:
    if result.refused:
        checks["V2"] = True
        return
    allowed = _allowed_values(pack)
    cited_text = _cited_passage_text(pack, _refs(rendered))
    unbound = [
        number
        for number in _numbers(rendered)
        if number not in allowed and number not in cited_text
    ]
    checks["V2"] = not unbound
    if unbound:
        errors.append("未绑定数字：" + ", ".join(sorted(set(unbound))))


def _check_v3(
    rendered: str,
    checks: dict[str, bool],
    errors: list[str],
    warnings: list[str],
    *,
    check_pages: bool,
) -> None:
    refs = _refs(rendered)
    if not check_pages:
        checks["V3"] = True
        return
    report_pages = verifier._report_pages()  # noqa: SLF001 - 复用既有页数据缓存
    if not report_pages:
        checks["V3"] = True
        warnings.append("没有解析数据，跳过引用页码校验。")
        return
    page_texts = verifier._page_texts()  # noqa: SLF001
    issues: list[str] = []
    for doc, page in refs:
        if doc not in report_pages:
            issues.append(f"引用文档不存在：{doc}")
        elif page < 1 or page > report_pages[doc]:
            issues.append(f"引用页码越界：{doc}#P{page}")
        elif (doc, page) not in page_texts:
            issues.append(f"引用页没有可检索文本：{doc}#P{page}")
    checks["V3"] = not issues
    errors.extend(issues)


def _check_v4(result: SynthesisResult, pack: EvidencePack, rendered: str, checks: dict[str, bool], errors: list[str]) -> None:
    if result.refused:
        checks["V4"] = True
        return
    refs = _refs(rendered)
    if not refs:
        checks["V4"] = False
        errors.append("答案缺少引用。")
        return
    if pack.tier == "document":
        issues: list[str] = []
        # 引用常写在句号之后（“句子。[Ref: …]”），先挪到句号前再按句检查。
        normalized = re.sub(r"([。！？；\n])((?:\[Ref:[^\]]+\])+)", r"\2\1", rendered)
        for sentence in _SENTENCE_SPLIT.split(normalized):
            sentence = sentence.strip()
            if not sentence or verifier.REF_RE.search(sentence):
                continue
            if _numbers(sentence) or len(sentence) >= _CONCLUSION_MIN_CHARS:
                issues.append("句子缺少引用：" + sentence[:24])
        checks["V4"] = not issues
        errors.extend(issues)
        return
    checks["V4"] = True


def _check_v5(rendered: str, pack: EvidencePack, checks: dict[str, bool], errors: list[str]) -> None:
    allowed = _whitelist(pack)
    outside = [f"{doc}#P{page}" for doc, page in _refs(rendered) if (doc, page) not in allowed]
    checks["V5"] = not outside
    if outside:
        errors.append("引用不在证据包白名单：" + ", ".join(outside))


def _check_v6(result: SynthesisResult, checks: dict[str, bool], errors: list[str]) -> None:
    if result.refused:
        checks["V6"] = bool(result.missing)
        if not result.missing:
            errors.append("拒答但没有说明缺少什么。")
        return
    checks["V6"] = bool(result.answer.strip())
    if not result.answer.strip():
        errors.append("答案为空。")


def _check_v7(rendered: str, pack: EvidencePack, checks: dict[str, bool], warnings: list[str]) -> None:
    table_pages = {(passage.doc, passage.page) for passage in pack.passages if passage.block_type == "table"}
    if not table_pages:
        checks["V7"] = True
        return
    refs = set(_refs(rendered))
    if refs and not (refs & table_pages):
        checks["V7"] = True
        warnings.append("证据包含整表块，但答案没有引用任何表格页。")
        return
    checks["V7"] = True


def verify_synthesis(
    result: SynthesisResult,
    pack: EvidencePack,
    *,
    check_pages: bool = True,
) -> VerificationResult:
    """对合成结果跑 V1–V7；error 级失败即 ``ok=False``。"""
    checks: dict[str, bool] = {}
    errors: list[str] = []
    warnings: list[str] = []
    rendered, _used = render(result.answer, _ledger_from_pack(pack))
    _check_v1(result, pack, checks, errors)
    _check_v2(result, pack, rendered, checks, errors)
    _check_v3(rendered, checks, errors, warnings, check_pages=check_pages)
    _check_v4(result, pack, rendered, checks, errors)
    _check_v5(rendered, pack, checks, errors)
    _check_v6(result, checks, errors)
    _check_v7(rendered, pack, checks, warnings)
    return VerificationResult(ok=not errors, checks=checks, errors=errors, warnings=warnings)


def compose_verified(
    question: str,
    pack: EvidencePack,
    backend: LLMBackend | None,
    *,
    max_retries: int = 1,
    check_pages: bool = True,
) -> tuple[SynthesisResult | None, VerificationResult]:
    """带校验的重试：失败带错误重试一次，仍失败返回 (None, 最后一次校验)。

    调用方拿到 ``None`` 时回退到抽取式回答（P4 接入）。
    """
    verification = VerificationResult(ok=False, errors=["没有可用的合成后端。"])
    if backend is None:
        return None, verification
    retry_note = ""
    for attempt in range(max_retries + 1):
        verification.attempts = attempt + 1
        system, user = build_prompt(question, pack, retry_note)
        try:
            response = backend.chat_json(system, user)
            result = normalize_response(response, pack)
        except Exception as exc:  # 超时/JSON 解析失败统一视为可重试失败
            retry_note = f"输出无法解析（{type(exc).__name__}）。"
            verification = VerificationResult(
                ok=False, checks={}, errors=[retry_note], warnings=[], attempts=attempt + 1
            )
            continue
        result.backend = getattr(backend, "name", "unknown")
        verification = verify_synthesis(result, pack, check_pages=check_pages)
        verification.attempts = attempt + 1
        if verification.ok:
            return result, verification
        retry_note = "；".join(verification.errors)
    return None, verification
