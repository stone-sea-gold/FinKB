#!/usr/bin/env python3
"""MCP 协议级联调：用真实 stdio JSON-RPC 验证多轮 context 与模式 B。

为什么要有这个脚本：``eval/check_context.py`` 和单元测试都是**进程内直接调
pipeline**，绕过了 MCP 适配层。真实宿主（DSH 等）看到的是 ``finkb-mcp`` 的
stdio 协议：一行一个 JSON-RPC 2.0 消息、按 id 配对、工具参数与返回值都要经
过一次 JSON 序列化。本脚本只走协议，验证这条链路上字段没丢、错误能上报。

用法::

    cd finkb && ../.venv/bin/python scripts/mcp_protocol_check.py

脚本自己起子进程（``.venv/bin/finkb-mcp``，缺失时退回
``python -m finkb.qa.mcp_main``），把 ``FINKB_HOME`` 指到本仓库 ``finkb/``
（数据路径相对仓库结构），逐条打印 PASS/FAIL。不联网、不新增依赖。

覆盖 7 条链路：

A. initialize 握手 + tools/list（14 个工具，含 8 个必需工具名）；
B. ask 指标题（schema_version / disclaimer / tier / 槽位）；
C. ask 多轮追问（previous_slots 继承公司+指标、覆盖期间、答案含 2024）；
D. 缺槽位时 clarify（不静默猜公司）；
E. evidence 证据包（passages/citations 非空并取首个 ref）；
F. verify 校验宿主答案（ok=true 且 V5=true）；
G. compare 三年并排 + 未知工具的 JSON-RPC error 上报。

退出码：0=全部通过；1=有失败（含子进程退出码非 0）。
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]  # finkb/：数据路径的锚点
FACT_DB = ROOT / "data" / "facts.duckdb"
VENV_MCP = ROOT.parent / ".venv" / "bin" / "finkb-mcp"

PROPOSED_PROTOCOL_VERSION = "2024-11-05"  # 服务端当前实现版本；实际以返回为准
STEP_TIMEOUT = 60.0  # 每个请求等响应的上限（秒）
EXPECTED_TOOL_COUNT = 14
REQUIRED_TOOLS = (
    "ask",
    "compare",
    "evidence",
    "verify",
    "query_metric",
    "search_documents",
    "describe_ontology",
    "traverse_relations",
)

QUESTION_METRIC = "比亚迪 2025 年营业收入是多少？"
QUESTION_FOLLOWUP = "那 2024 年呢？"
QUESTION_CLARIFY = "营业收入是多少？"
QUESTION_DOC = "比亚迪 2025 年报里，公司如何描述海外市场的区域布局？"
QUESTION_COMPARE = "美的集团 2023-2025 年营业收入"


class MCPError(RuntimeError):
    """协议层/进程层错误：超时、stdout 关闭、写管道失败等。"""


def server_command() -> list[str]:
    """优先用 venv 的 finkb-mcp；不存在时用当前解释器跑模块入口。"""
    if VENV_MCP.exists():
        return [str(VENV_MCP)]
    return [sys.executable, "-m", "finkb.qa.mcp_main"]


class MCPClient:
    """最小 MCP stdio 客户端：newline-delimited JSON-RPC 2.0，按 id 配对。

    - stdout 只当 JSON-RPC 用；非 JSON 行（服务端误写的日志）记进
      :attr:`noise_lines` 后跳过，不打断解析。
    - stderr 落到临时文件而不是管道，避免日志写满 64KB 把服务端卡死。
    - 每条请求有截止时间，超时抛 :class:`MCPError`（带方法名）。
    """

    def __init__(self, command: list[str] | None = None, *, cwd: Path | None = None,
                 timeout: float = STEP_TIMEOUT) -> None:
        self.command = list(command) if command else server_command()
        self.cwd = Path(cwd) if cwd else ROOT
        self.timeout = float(timeout)
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.noise_lines: list[str] = []
        self.returncode: int | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr: Any = None
        self._stderr_text = ""
        self._buf = b""
        self._next_id = 0
        self._last_method = ""
        self._init_result: dict[str, Any] | None = None

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> "MCPClient":
        env = dict(os.environ)
        # 数据路径相对仓库结构：把知识库锚定到 finkb/，不受调用者 cwd 影响。
        env["FINKB_HOME"] = str(ROOT)
        src = str(ROOT / "src")
        env["PYTHONPATH"] = src + ((os.pathsep + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
        self._stderr = tempfile.TemporaryFile()
        self._proc = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
        )
        self._buf = b""
        return self

    def close(self, timeout: float = 10.0) -> int | None:
        """关 stdin（服务端读到 EOF 退出），返回退出码；超时则 kill。"""
        if self._proc is None:
            return self.returncode
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            code = self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            code = self._proc.wait(timeout=5)
        self.returncode = code
        self._stderr_text = self.stderr_tail()
        if self._stderr is not None:
            try:
                self._stderr.close()
            except OSError:
                pass
            self._stderr = None
        return code

    def __enter__(self) -> "MCPClient":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 传输 -------------------------------------------------------------

    def _send(self, message: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("客户端未启动")
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(
                f"写入失败（服务端可能已退出，exit={self._proc.poll()}）：{exc}"
            ) from exc

    def _read_line(self, deadline: float) -> str:
        while True:
            index = self._buf.find(b"\n")
            if index >= 0:
                raw, self._buf = self._buf[:index], self._buf[index + 1:]
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    return line
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(f"等待响应超时（{self.timeout:.0f}s）：{self._last_method}")
            if self._proc is None or self._proc.stdout is None:
                raise MCPError("客户端未启动")
            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                raise MCPError(f"等待响应超时（{self.timeout:.0f}s）：{self._last_method}")
            chunk = os.read(self._proc.stdout.fileno(), 65536)
            if not chunk:
                raise MCPError(
                    f"服务端 stdout 已关闭（exit={self._proc.poll()}）：{self._last_method}"
                )
            self._buf += chunk

    def _await(self, request_id: int, method: str) -> dict:
        deadline = time.monotonic() + self.timeout
        while True:
            line = self._read_line(deadline)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # stdout 上的日志噪音：跳过，但留证据。
                self.noise_lines.append(line)
                continue
            if not isinstance(message, dict):
                self.noise_lines.append(line)
                continue
            if message.get("id") != request_id:
                continue  # 不是本次请求的响应（迟到包/服务端主动消息）
            return message

    # -- JSON-RPC 方法 ----------------------------------------------------

    def request(self, method: str, params: dict | None = None, *, label: str | None = None) -> dict:
        """发一条请求并返回整条响应（含 error 字段），由调用方判 error。"""
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._last_method = label or method
        self._send(message)
        return self._await(request_id, method)

    def _result(self, method: str, params: dict | None = None) -> dict:
        response = self.request(method, params)
        if "error" in response:
            error = response.get("error") or {}
            raise MCPError(
                f"{method} 返回 JSON-RPC error {error.get('code')}: {error.get('message')}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPError(f"{method} 响应缺少 result 对象：{response}")
        return result

    def notify(self, method: str, params: dict | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def initialize(self) -> dict:
        """initialize + notifications/initialized；重复调用返回首次结果。

        protocolVersion 提议 :data:`PROPOSED_PROTOCOL_VERSION`，以服务端
        返回的为准（本项目服务端固定实现 2024-11-05）。
        """
        if self._init_result is not None:
            return self._init_result
        result = self._result(
            "initialize",
            {
                "protocolVersion": PROPOSED_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "finkb-mcp-protocol-check", "version": "0.1.0"},
            },
        )
        version = result.get("protocolVersion")
        if not isinstance(version, str) or not version:
            raise MCPError(f"initialize 未返回 protocolVersion：{result}")
        if version != PROPOSED_PROTOCOL_VERSION:
            print(f"NOTE 服务端协议版本 {version} ≠ 提议 {PROPOSED_PROTOCOL_VERSION}，按返回继续。")
        self.protocol_version = version
        self.server_info = result.get("serverInfo") or {}
        self.notify("notifications/initialized")
        self._init_result = result
        return result

    def list_tools(self) -> list[dict]:
        result = self._result("tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise MCPError(f"tools/list 返回的 tools 不是列表：{result}")
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict | None = None) -> tuple[dict, bool]:
        """调用一个工具，返回 ``(payload, is_error)``。

        JSON-RPC 层 error（未知工具、必填参数缺失）折算成
        ``({"error": {...}}, True)``；``result.isError=true`` 时解析其文本
        内容后同样返回 ``True``。
        """
        response = self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            label=f"tools/call {name}",
        )
        if "error" in response:
            return {"error": response.get("error") or {}}, True
        result = response.get("result") or {}
        is_error = bool(result.get("isError"))
        text = "".join(
            str(item.get("text") or "")
            for item in (result.get("content") or [])
            if isinstance(item, dict)
        )
        if not text:
            return {}, is_error
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}, True
        if not isinstance(payload, dict):
            return {"raw": payload}, is_error
        return payload, is_error

    # -- 诊断 -------------------------------------------------------------

    def stderr_tail(self, limit: int = 4000) -> str:
        if self._stderr is None:
            return self._stderr_text
        try:
            self._stderr.flush()
            self._stderr.seek(0)
            data = self._stderr.read()
        except (OSError, ValueError):
            return self._stderr_text
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        return text[-limit:].strip()


# -- 断言用的纯函数 -------------------------------------------------------


def slots_of(payload: dict) -> dict:
    """取本轮结构化槽位：优先 ``resolved_slots``，其次 ``data.slots``，最后 ``slots``。

    实测：ask 的指标档三个字段里 ``resolved_slots`` 与顶层 ``slots`` 都有，
    ``data.slots`` 为 null；这里按优先级兜底，避免绑定到单一字段名。
    """
    candidates = (
        payload.get("resolved_slots"),
        (payload.get("data") or {}).get("slots"),
        payload.get("slots"),
    )
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


_REF_RE = re.compile(r"\[Ref:\s*([^\]]+?)\s*\]")


def first_ref(citations: list) -> str | None:
    """从 citations 里取第一个 ref（``[Ref: 002594_2025_年报#P25]`` → ``002594_2025_年报#P25``）。"""
    for item in citations or []:
        text = str(item or "").strip()
        match = _REF_RE.search(text)
        if match:
            return match.group(1)
        if "#P" in text:
            return text
    return None


# -- 7 条链路 -------------------------------------------------------------


def chain_a_handshake_and_tools(client: MCPClient) -> tuple[bool, list[str]]:
    details: list[str] = []
    info = client.initialize()
    details.append(f"protocolVersion={client.protocol_version}（提议 {PROPOSED_PROTOCOL_VERSION}）")
    details.append(f"serverInfo={json.dumps(client.server_info, ensure_ascii=False)}")

    ok = bool(client.server_info.get("name")) and bool(client.server_info.get("version"))
    tools = client.list_tools()
    names = [str(tool.get("name")) for tool in tools]
    details.append(f"tools/list={len(names)} 个：{', '.join(names)}")
    if len(names) != EXPECTED_TOOL_COUNT:
        ok = False
        details.append(f"工具数 {len(names)} ≠ {EXPECTED_TOOL_COUNT}")
    missing = [name for name in REQUIRED_TOOLS if name not in names]
    if missing:
        ok = False
        details.append("缺少必需工具：" + ", ".join(missing))
    if not info.get("capabilities"):
        details.append("NOTE capabilities 为空（当前服务端只声明 tools，可接受）")
    return ok, details


def chain_b_ask_metric(client: MCPClient) -> tuple[bool, list[str]]:
    payload, is_error = client.call_tool("ask", {"question": QUESTION_METRIC})
    if is_error:
        return False, [f"工具报错：{json.dumps(payload, ensure_ascii=False)}"]

    ok = True
    details = [
        f"tier={payload.get('tier')} refused={payload.get('refused')} "
        f"confidence={payload.get('confidence')}",
        f"schema_version={payload.get('schema_version')!r} "
        f"disclaimer={'有' if payload.get('disclaimer') else '无'}",
    ]
    if not isinstance(payload.get("schema_version"), str):
        ok = False
        details.append("缺 schema_version")
    if not payload.get("disclaimer"):
        ok = False
        details.append("缺 disclaimer")
    if payload.get("tier") != "metric":
        ok = False
        details.append(f"tier={payload.get('tier')} ≠ metric")

    slots = slots_of(payload)
    details.append(f"slots={json.dumps(slots, ensure_ascii=False)}")
    details.append(f"text={str(payload.get('text') or '')[:80]}")
    if slots.get("metric") != "revenue":
        ok = False
        details.append(f"slots.metric={slots.get('metric')!r} ≠ revenue")
    if "2025" not in [str(p) for p in slots.get("periods") or []]:
        ok = False
        details.append(f"slots.periods={slots.get('periods')!r} 不含 2025")
    return ok, details


def chain_c_followup_context(client: MCPClient) -> tuple[bool, list[str]]:
    first, is_error = client.call_tool("ask", {"question": QUESTION_METRIC})
    if is_error:
        return False, [f"第一轮报错：{json.dumps(first, ensure_ascii=False)}"]
    previous_slots = slots_of(first)
    payload, is_error = client.call_tool(
        "ask",
        {
            "question": QUESTION_FOLLOWUP,
            "context": {"previous_slots": previous_slots, "previous_question": QUESTION_METRIC},
        },
    )
    if is_error:
        return False, [f"第二轮报错：{json.dumps(payload, ensure_ascii=False)}"]

    ok = True
    slots = slots_of(payload)
    inherited = [str(item) for item in payload.get("inherited") or []]
    overridden = [str(item) for item in payload.get("overridden") or []]
    periods = [str(p) for p in slots.get("periods") or []]
    text = str(payload.get("text") or "")
    details = [
        f"tier={payload.get('tier')} refused={payload.get('refused')}",
        f"resolved_question={payload.get('resolved_question')}",
        f"resolved_slots={json.dumps(slots, ensure_ascii=False)}",
        f"inherited={inherited} overridden={overridden}",
        f"text={text[:100]}",
    ]
    if payload.get("tier") != "metric":
        ok = False
        details.append(f"tier={payload.get('tier')} ≠ metric")
    if "2024" not in periods:
        ok = False
        details.append(f"periods={periods} 不含 2024")
    if not {"company", "metric"} <= set(inherited):
        ok = False
        details.append(f"inherited={inherited} 未含 company+metric")
    if "period" not in overridden:
        ok = False
        details.append(f"overridden={overridden} 未含 period")
    if "2024" not in text:
        ok = False
        details.append("答案文本不含 2024")
    if not any(ch.isdigit() for ch in text):
        ok = False
        details.append("答案文本没有数值")
    return ok, details


def chain_d_clarify(client: MCPClient) -> tuple[bool, list[str]]:
    payload, is_error = client.call_tool("ask", {"question": QUESTION_CLARIFY})
    if is_error:
        return False, [f"工具报错：{json.dumps(payload, ensure_ascii=False)}"]

    clarify = payload.get("clarify")
    clarify = clarify if isinstance(clarify, dict) else {}
    missing = [str(item) for item in (clarify.get("missing") or payload.get("missing") or [])]
    slots = slots_of(payload)
    refused = bool(payload.get("refused"))
    text = str(payload.get("text") or "")
    details = [
        f"refused={refused} tier={payload.get('tier')}",
        f"missing={missing}",
        f"clarify={json.dumps(clarify, ensure_ascii=False)[:300]}",
        f"text={text[:100]}",
        f"slots.companies={slots.get('companies')!r}",
    ]
    ok = True
    if not {"company", "period"} <= set(missing):
        ok = False
        details.append("missing 未同时包含 company/period")
    if not (refused or clarify):
        ok = False
        details.append("既没拒答也没给 clarify")
    if slots.get("companies"):
        ok = False
        details.append(f"缺公司却猜了公司：{slots.get('companies')}")
    if payload.get("ledger"):
        ok = False
        details.append("缺槽位却返回了数字账本（静默作答）")
    return ok, details


def _evidence_payload(client: MCPClient) -> tuple[dict, bool]:
    return client.call_tool("evidence", {"question": QUESTION_DOC, "tier": "document"})


def chain_e_evidence(client: MCPClient) -> tuple[bool, list[str]]:
    payload, is_error = _evidence_payload(client)
    if is_error:
        return False, [f"工具报错：{json.dumps(payload, ensure_ascii=False)}"]

    evidence = payload.get("evidence") or {}
    passages = evidence.get("passages") or []
    citations = evidence.get("citations") or []
    ref = first_ref(citations)
    details = [
        f"mode={payload.get('mode')} tier={payload.get('tier')} refused={payload.get('refused')}",
        f"counts={{'passages': {len(passages)}, 'citations': {len(citations)}, "
        f"'facts': {len(evidence.get('facts') or [])}}}",
        f"citations={citations}",
        f"首个 ref={ref}",
        f"首个 passage={json.dumps(passages[0], ensure_ascii=False)[:160]}" if passages else "没有 passage",
    ]
    ok = bool(passages) and bool(citations) and ref is not None
    if payload.get("mode") != "B":
        ok = False
        details.append(f"mode={payload.get('mode')} ≠ B")
    return ok, details


def chain_f_verify(client: MCPClient) -> tuple[bool, list[str]]:
    evidence_payload, is_error = _evidence_payload(client)
    if is_error:
        return False, [f"evidence 报错：{json.dumps(evidence_payload, ensure_ascii=False)}"]
    ref = first_ref((evidence_payload.get("evidence") or {}).get("citations") or [])
    if ref is None:
        return False, ["evidence 没给出可用的 ref"]

    text = f"公司持续推进海外布局。 [Ref: {ref}]"
    details = [f"ref={ref}", f"answer={text}"]

    # 顺带记录协议事实：服务端 normalize_response 只认 answer 键；传 text 会被
    # 当成“缺少 answer”而报 isError。schema 本身写的就是 answer。
    legacy = {"text": text, "used_ids": [], "refused": False, "missing": [], "notes": []}
    legacy_payload, legacy_error = client.call_tool(
        "verify", {"question": QUESTION_DOC, "tier": "document", "answer": legacy}
    )
    details.append(
        f"NOTE 用 text 键 → isError={legacy_error}，"
        f"返回 {json.dumps(legacy_payload, ensure_ascii=False)[:120]}"
    )

    answer = {"answer": text, "used_ids": [], "refused": False, "missing": [], "notes": []}
    payload, is_error = client.call_tool(
        "verify", {"question": QUESTION_DOC, "tier": "document", "answer": answer}
    )
    if is_error:
        details.append(f"verify 报错：{json.dumps(payload, ensure_ascii=False)}")
        return False, details

    checks = payload.get("checks") or {}
    details.append(f"ok={payload.get('ok')} checks={json.dumps(checks, ensure_ascii=False)}")
    details.append(f"errors={payload.get('errors')} warnings={payload.get('warnings')}")
    ok = payload.get("ok") is True and checks.get("V5") is True
    if payload.get("mode") != "B":
        ok = False
        details.append(f"mode={payload.get('mode')} ≠ B")
    return ok, details


def chain_g_compare_and_error(client: MCPClient) -> tuple[bool, list[str]]:
    payload, is_error = client.call_tool("compare", {"question": QUESTION_COMPARE})
    details: list[str] = []
    ok = True
    if is_error:
        return False, [f"compare 报错：{json.dumps(payload, ensure_ascii=False)}"]

    data = payload.get("data") or {}
    rows = data.get("rows") or []
    periods = [str(item) for item in data.get("periods") or []]
    details.append(f"periods={periods}")
    details.append(f"rows={len(rows)} 条")
    for row in rows:
        details.append(
            f"  {row.get('period')}: value={row.get('value')!r} unit={row.get('unit')!r} "
            f"doc={row.get('doc')} P{row.get('page')}"
        )
    if len(rows) != 3:
        ok = False
        details.append(f"rows={len(rows)} ≠ 3")
    if periods != ["2023", "2024", "2025"]:
        ok = False
        details.append(f"periods={periods} ≠ ['2023','2024','2025']")
    for row in rows:
        if "value" not in row or row.get("value") is None:
            ok = False
            details.append(f"{row.get('period')} 的 value 为空")

    # 未知工具必须被协议层正确地报成错误，而不是静默成功。
    error_payload, error_is_error = client.call_tool("no_such_tool", {})
    details.append(
        f"未知工具 no_such_tool → isError={error_is_error}，"
        f"{json.dumps(error_payload, ensure_ascii=False)[:160]}"
    )
    if not error_is_error:
        ok = False
        details.append("未知工具没有报错")
    return ok, details


CHAINS: list[tuple[str, Any]] = [
    ("A. initialize + tools/list（14 个工具）", chain_a_handshake_and_tools),
    ("B. ask 指标题（schema_version/disclaimer/tier/槽位）", chain_b_ask_metric),
    ("C. ask 多轮追问（继承公司+指标、覆盖期间）", chain_c_followup_context),
    ("D. 缺槽位 clarify（不静默猜公司）", chain_d_clarify),
    ("E. evidence 证据包（passages/citations）", chain_e_evidence),
    ("F. verify 宿主答案（ok/V5）", chain_f_verify),
    ("G. compare 三年并排 + 未知工具报错", chain_g_compare_and_error),
]


def main() -> int:
    print("MCP 协议级联调（真实 stdio JSON-RPC）")
    print(f"  服务命令：{' '.join(server_command())}")
    print(f"  工作目录：{ROOT}")
    print(f"  知识库：{ROOT / 'data' / 'facts.duckdb'}")
    if not FACT_DB.exists():
        print(f"[FAIL] 前置条件：{FACT_DB} 不存在")
        return 1

    chain_failures = 0
    client = MCPClient()
    exit_code: int | None = None
    try:
        client.start()
        for title, chain in CHAINS:
            try:
                ok, details = chain(client)
            except (MCPError, OSError) as exc:
                ok, details = False, [f"{type(exc).__name__}: {exc}"]
            except Exception as exc:  # noqa: BLE001 - 单条链路异常不应中断其余检查
                ok, details = False, [f"{type(exc).__name__}: {exc}"]
            chain_failures += 0 if ok else 1
            print(f"\n[{'PASS' if ok else 'FAIL'}] {title}")
            for line in details:
                print(f"       {line}")
    except (MCPError, OSError) as exc:
        chain_failures = len(CHAINS)
        print(f"\n[FAIL] 启动/握手失败：{type(exc).__name__}: {exc}")
    finally:
        exit_code = client.close()

    if client.noise_lines:
        print(
            f"\nNOTE stdout 上出现 {len(client.noise_lines)} 行非 JSON-RPC 内容（已跳过）："
            f"{client.noise_lines[:2]}"
        )
    exit_ok = exit_code == 0
    print(f"\n[{'PASS' if exit_ok else 'FAIL'}] 子进程退出码：{exit_code}")
    if not exit_ok:
        tail = client.stderr_tail()
        if tail:
            print("       stderr 末尾：" + tail.replace("\n", " | ")[-500:])

    total = len(CHAINS)
    passed = total - chain_failures
    print(f"\n协议联调：{passed}/{total} 条链路通过；子进程退出码 {exit_code}")
    return 0 if chain_failures == 0 and exit_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
