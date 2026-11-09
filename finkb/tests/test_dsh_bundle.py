"""DSH 发布包（dsh-finkb/）的静态契约测试。

这里只校验"接线层"的关键声明，不启动 Node/DSH：
- package.json 的 bundle/client/exports 字段；
- cordis.patch.yml 必须注册 MCP 服务与设置接口插件；
- 设置页客户端/服务端文件与关键接口路径存在。

真正的运行时验证在 `dsh-finkb/README.md` 的"装好后怎么验证"里。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "dsh-finkb"


def _read(name: str) -> str:
    return (BUNDLE / name).read_text(encoding="utf-8")


def test_package_manifest_declares_bundle_and_client() -> None:
    manifest = json.loads(_read("package.json"))
    assert manifest["name"] == "finkb-dsh"
    assert manifest["type"] == "module"
    assert manifest["main"] == "./lib/index.js"
    assert manifest["exports"]["./client"] == "./lib/client.js"
    assert manifest["dsh"]["bundle"]["patch"] == [
        "./cordis.patch.yml",
        "./presets/finkb.patch.yml",
    ]
    assert manifest["dsh"]["client"]["platform"] == "web"
    # 设置页客户端需要 React；服务端插件需要 schemastery。
    for name in ("react", "react-dom", "@deepseek-ai/schemastery", "@deepseek-ai/cordis"):
        assert name in manifest["peerDependencies"], name
    assert "lib" in manifest["files"]


def test_patch_registers_mcp_and_settings_plugin() -> None:
    patch = _read("cordis.patch.yml")
    assert "id: mcp-finkb" in patch
    assert "@deepseek-ai/dsh-mcp-client" in patch
    assert "serverName: finkb" in patch
    assert "transport: stdio" in patch
    assert "process.env.FINKB_MCP_BIN" in patch
    assert "id: finkb-settings" in patch
    assert "name: 'finkb-dsh'" in patch


def test_preset_declares_report_assistant() -> None:
    preset = _read("presets/finkb.patch.yml")
    assert "@deepseek-ai/dsh-agent-preset" in preset
    assert "name: 财报助手" in preset
    assert "finkb-dsh/package.json" in preset  # 技能目录用包内路径解析


def test_settings_files_and_endpoint_exist() -> None:
    server = _read("lib/index.js")
    client = _read("lib/client.js")
    env = _read("lib/env.js")
    assert "export const name = 'finkb-settings'" in server
    assert "/finkb-settings/api" in server
    assert "webServer.register" in server
    assert "settings.section" in client
    assert "label: 'FinKB'" in client
    # 知识库目录是部署期配置，卡片不再提供运行期修改入口
    assert "finkbHome" not in client
    assert "LLM_API_KEY" in env
    assert "SILICONFLOW_API_KEY" in env
    assert "MINERU_API_TOKEN" in env


def test_skill_file_present() -> None:
    skill = BUNDLE / "skills" / "finkb-qa" / "SKILL.md"
    assert skill.is_file()
    text = skill.read_text(encoding="utf-8")
    assert "finkb-qa" in text
    assert "mcp__finkb__library_status" in text


@pytest.mark.parametrize("name", ["lib/env.js", "lib/index.js", "lib/client.js", "scripts/check-env-merge.mjs"])
def test_node_sources_present(name: str) -> None:
    assert (BUNDLE / name).is_file()


def test_client_shows_import_and_key_advice():
    client = (BUNDLE / "lib" / "client.js").read_text(encoding="utf-8")
    assert "导入年报" in client
    assert "建议填写" in client
    assert "rawDir" in client
