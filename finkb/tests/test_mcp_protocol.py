"""MCP 协议级联调的 pytest 版本。

和 ``scripts/mcp_protocol_check.py`` 覆盖同一批链路，区别是：那边是给 CI/人工
跑的一键脚本（自己起进程、打印 PASS/FAIL、用退出码表态），这边是 pytest 用例，
失败时能定位到具体断言。两者共用同一份客户端实现，避免协议细节写两遍。

运行::

    cd finkb && ../.venv/bin/python -m pytest tests/test_mcp_protocol.py -o addopts="" -q

为什么需要它：现有的 ``eval/check_context.py`` 与单元测试都在**进程内**直接调
pipeline，绕过 MCP 适配层；这里只走 subprocess + stdio JSON-RPC，验证宿主真正
看到的那条路径（字段序列化、id 配对、错误上报）没有退化。

前置条件不满足时整文件 skip：没有知识库 ``data/facts.duckdb``，或没有
``.venv/bin/finkb-mcp``（脚本会退回 ``python -m finkb.qa.mcp_main``，但按约定
仍跳过，避免在裸环境里误报）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]  # finkb/
SCRIPT = ROOT / "scripts" / "mcp_protocol_check.py"
FACT_DB = ROOT / "data" / "facts.duckdb"
VENV_MCP = ROOT.parent / ".venv" / "bin" / "finkb-mcp"

pytestmark = pytest.mark.skipif(
    not FACT_DB.exists() or not VENV_MCP.exists(),
    reason=f"需要 {FACT_DB} 与 {VENV_MCP}（离线知识库 + venv 入口）",
)


def _load_check_module():
    """按文件路径加载脚本；脚本有 __main__ 保护，import 不会执行 main()。"""
    spec = importlib.util.spec_from_file_location("mcp_protocol_check", SCRIPT)
    assert spec and spec.loader, f"无法加载 {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcp_protocol_check"] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


@pytest.fixture(scope="module")
def client():
    """整个模块共用一个子进程：真实宿主也是长连接，多轮上下文才有意义。"""
    instance = check.MCPClient()
    instance.start()
    try:
        instance.initialize()
        yield instance
    finally:
        exit_code = instance.close()
        assert exit_code == 0, f"服务端退出码 {exit_code}；stderr 末尾：{instance.stderr_tail()}"


def _report(ok: bool, details: list[str]) -> str:
    return "\n".join(details) if not ok else ""


def _assert_chain(chain, client):
    ok, details = chain(client)
    assert ok, _report(ok, details)
    return details


# -- A. 握手与工具清单 ----------------------------------------------------


def test_initialize_handshake(client):
    """initialize 成功，且服务端报出名字和版本。"""
    assert client.protocol_version, "没有 protocolVersion"
    info = client.server_info
    assert info.get("name") == "finkb-qa", f"serverInfo.name={info.get('name')!r}"
    assert info.get("version"), f"serverInfo.version 为空：{info}"


def test_tools_list_count_and_names(client):
    """tools/list 返回 14 个工具，必需的 8 个都在。"""
    tools = client.list_tools()
    names = [str(tool.get("name")) for tool in tools]
    assert len(names) == check.EXPECTED_TOOL_COUNT, f"工具数 {len(names)}：{names}"
    missing = [name for name in check.REQUIRED_TOOLS if name not in names]
    assert not missing, f"缺少必需工具 {missing}；实际：{names}"


# -- B/C. ask 与多轮 context ---------------------------------------------


def test_ask_metric(client):
    """指标题：schema_version / disclaimer / tier，并给出可复用的槽位。"""
    details = _assert_chain(check.chain_b_ask_metric, client)
    assert details  # 保留实际返回，失败时 pytest 会打印


def test_ask_followup_context(client):
    """追问只有「那 2024 年呢？」：公司和指标从上一轮继承，期间被覆盖。"""
    _assert_chain(check.chain_c_followup_context, client)


def test_clarify_when_slots_missing(client):
    """缺公司/期间时必须反问或拒答，不能静默挑一个公司答数字。"""
    _assert_chain(check.chain_d_clarify, client)


# -- E/F. 模式 B：证据包与校验 -------------------------------------------


def test_evidence_document_tier(client):
    """document 档返回证据包：passages 与 citations 都非空，且能取出 ref。"""
    _assert_chain(check.chain_e_evidence, client)


def test_verify_host_answer(client):
    """宿主答案带合法 ref 时校验通过：ok=true 且 V5=true。"""
    details = _assert_chain(check.chain_f_verify, client)
    assert any("V5" in line for line in details), f"checks 里没有 V5：{details}"


# -- G. compare 与错误上报 ------------------------------------------------


def test_compare_three_years(client):
    """三年并排：3 行、期间齐全、每行 value 非空。"""
    _assert_chain(check.chain_g_compare_and_error, client)
