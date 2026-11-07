"""合成子系统（P1：契约骨架 + FakeBackend，不接真模型、不改现有行为）。

设计见 ``docs/合成子系统设计.md``。合成器的输入是
:class:`finkb.qa.evidence.EvidencePack`，输出是受约束的
:class:`SynthesisResult`：

- 数字只能写 ``{{cN}}`` 占位符，由 :func:`finkb.qa.synthesizer.render` 填值；
- 每句事实性陈述必须带 ``[Ref: doc#P页码]``；
- 证据不足必须 ``refused=true``；
- 合成失败由调用方回退到抽取式回答（P1 尚未接入 pipeline）。

P1 只做接口与离线 FakeBackend；真实 LLM 后端、校验、重试在 P2/P3 接入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from finkb.qa.evidence import EvidencePack

SYSTEM_PROMPT = """你是 FinKB 的财报问答写手。你只能使用下面给出的证据，不得使用外部知识。
规则：
1. 只写证据支持的结论；证据没说的因果、预测、评价一律不写。
2. 所有数字必须写成占位符 {{cN}}（N 为证据编号），不得直接写金额、比例、数量。
3. 每个事实性句子末尾必须带 [Ref: 文档#P页码]，只能引用证据里出现过的文档和页码。
4. 证据不足时不要猜，输出 refused=true 并说明缺少什么。
5. 回答用中文，简洁；metric 档先给数字句，再给一句解读，解读不得引入新数字或新事实。
6. document 档回答 2–5 句、不超过 400 字。
7. 只输出 JSON，不要输出 Markdown 代码围栏或额外解释。"""

OUTPUT_SCHEMA = """{"answer": "正文（数字用 {{cN}}）", "used_ids": ["c1", "e2"],
 "refused": false, "missing": ["缺少的证据"], "notes": ["需要提醒用户的口径"]}"""


@dataclass
class SynthesisResult:
    """合成结果（P1 契约；P3 起会再挂校验结果）。"""

    answer: str
    used_ids: list[str] = field(default_factory=list)
    refused: bool = False
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    backend: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "used_ids": list(self.used_ids),
            "refused": self.refused,
            "missing": list(self.missing),
            "notes": list(self.notes),
            "backend": self.backend,
        }


class LLMBackend(Protocol):
    """合成后端：只要求一个返回 JSON 对象的 ``chat_json``。"""

    name: str

    def chat_json(self, system: str, user: str) -> dict[str, Any]: ...


def render_evidence(pack: EvidencePack) -> str:
    """把证据包渲染成提示词里的证据清单。"""
    lines: list[str] = []
    if pack.facts:
        lines.append("【结构化事实】")
        for fact in pack.facts:
            head = f"[{fact.id}] {fact.company} {fact.period} {fact.metric_name or fact.metric_id} = {fact.value:g} {fact.unit}".strip()
            if fact.kind == "derived":
                head += f"（派生，公式：{fact.formula or '见账本'}）"
            elif fact.kind == "alternative":
                head += f"（并列口径，{fact.role or '其他来源'}）"
            if fact.doc:
                head += f" 来源 {fact.doc}#P{fact.page}"
            lines.append(head)
    if pack.inferences:
        lines.append("【本体推断】")
        for item in pack.inferences:
            lines.append(f"[{item.id}] {item.kind}:{item.rule_id} {item.text}".strip())
    if pack.passages:
        lines.append("【原文证据】")
        for item in pack.passages:
            kind = "表格" if item.block_type == "table" else "正文"
            section = f" §{item.section}" if item.section else ""
            lines.append(f"[{item.id}] {item.doc}#P{item.page}{section}（{kind}）\n{item.text}")
    if pack.conflicts:
        lines.append("【口径冲突】")
        for item in pack.conflicts:
            lines.append(
                f"[{item.id}] {item.metric_id} {item.company} {item.period}: "
                + " / ".join(f"{value:g}" for value in item.values)
                + f"（{', '.join(item.docs)}）"
            )
    if pack.notes:
        lines.append("【系统提示】")
        lines.extend(f"- {note}" for note in pack.notes)
    return "\n".join(lines) if lines else "（没有可用证据）"


def build_prompt(question: str, pack: EvidencePack) -> tuple[str, str]:
    """返回 (system, user) 提示词。"""
    user = (
        f"问题：{question}\n\n"
        f"证据：\n{render_evidence(pack)}\n\n"
        f"请按 JSON 输出：{OUTPUT_SCHEMA}"
    )
    return SYSTEM_PROMPT, user


class FakeBackend:
    """离线测试用后端：按预设返回 JSON，不联网。"""

    name = "fake"

    def __init__(self, responses: dict[str, Any] | list[dict[str, Any]] | None = None) -> None:
        if isinstance(responses, list):
            self._queue = list(responses)
        elif isinstance(responses, dict):
            self._queue = [responses]
        else:
            self._queue = []
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        if self._queue:
            return self._queue.pop(0)
        return {
            "answer": "（离线占位：合成器尚未接入真实模型）",
            "used_ids": [],
            "refused": False,
            "missing": [],
            "notes": ["fake-backend"],
        }


def _normalize(response: dict[str, Any], pack: EvidencePack) -> SynthesisResult:
    if not isinstance(response, dict):
        raise ValueError("合成结果必须是 JSON 对象")
    answer = response.get("answer")
    refused = bool(response.get("refused", False))
    if not isinstance(answer, str) or (not answer.strip() and not refused):
        raise ValueError("合成结果缺少 answer")
    used_ids = response.get("used_ids") or []
    if not isinstance(used_ids, list):
        raise ValueError("used_ids 必须是列表")
    known = pack.ids()
    notes = [str(item) for item in (response.get("notes") or [])]
    unknown = [str(item) for item in used_ids if str(item) not in known]
    if unknown:
        notes.append("未知证据 id：" + ", ".join(unknown))
    missing = [str(item) for item in (response.get("missing") or [])]
    return SynthesisResult(
        answer=answer,
        used_ids=[str(item) for item in used_ids if str(item) in known],
        refused=refused,
        missing=missing,
        notes=notes,
    )


def compose(
    question: str,
    pack: EvidencePack,
    backend: LLMBackend | None,
    *,
    max_retries: int = 0,
) -> SynthesisResult | None:
    """调用后端生成受约束答案；无后端或全部失败时返回 None（调用方回退）。"""
    if backend is None:
        return None
    system, user = build_prompt(question, pack)
    last_error: Exception | None = None
    for _attempt in range(max_retries + 1):
        try:
            response = backend.chat_json(system, user)
            result = _normalize(response, pack)
            result.backend = getattr(backend, "name", "unknown")
            return result
        except Exception as exc:  # 后端错误/格式错误都视为可回退失败
            last_error = exc
    if last_error is not None:
        return None
    return None


def synthesize(question: str, pack: EvidencePack, backend: LLMBackend | None = None) -> SynthesisResult | None:
    """便捷入口：P2 起由 pipeline 调用；P1 不接入。"""
    return compose(question, pack, backend)
