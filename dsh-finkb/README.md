# finkb-dsh

把本地 [FinKB](../../README.md) A 股年报知识库接入 DSH。装完后你会得到：

- **插件页**多一项 `finkb-dsh`（默认启用）；
- 模型工具列表里多出 14 个 `mcp__finkb__*` 工具；
- 新会话的预设选择器里多一个 **「财报助手」**（人格 + 技能 + 工具路由说明）；
- **设置页**左侧多一个「FinKB」：在页面里填 LLM / SiliconFlow / MinerU 密钥，保存即生效（写 `$FINKB_HOME/config/.env`，不用重启）。

本包**不含**任何业务逻辑、年报 PDF、事实库或向量索引。所有数据在本地 `FINKB_HOME` 里，由使用者自己建库。

## 前置条件

1. **DSH**：`>= 0.2.2-rc.1 < 0.3.0`（本包只在 0.2.2-rc.1 上验证过），Node `>= 20`。
2. **Python 3.10+**，并安装 FinKB（提供 `finkb-mcp` 可执行文件）：

   ```bash
   # 发布后（推荐）
   pip install finkb
   # 或 pipx install finkb

   # 当前仓库开发态
   cd finkb && ../.venv/bin/pip install -e ".[search,ontology]"
   ```

3. **知识库目录**（没有就初始化）：

   ```bash
   export FINKB_HOME="$HOME/.finkb"     # 不设置时 pip 安装默认 ~/.finkb
   python -m finkb.library init
   # 编辑 $FINKB_HOME/config/.env，至少填 LLM_API_KEY
   ```

4. 确认服务能启动：

   ```bash
   finkb-mcp --help 2>/dev/null || echo "检查 PATH 或设置 FINKB_MCP_BIN"
   ```

## 安装

### 方式 A：从 npm 安装（发布后）

在 DSH Web 侧边栏「插件」页安装 `finkb-dsh@0.2.0`；或者用 CLI：

```bash
dsh plugin --profile web add finkb-dsh@0.2.0
# 然后在插件页确认 finkb-dsh 已启用（bundle 需要出现在 dsh.profile.bundles 中）
```

### 方式 B：本地开发安装

```bash
dsh plugin --profile web add file:/absolute/path/to/dsh-finkb
# 同样在插件页启用
```

### 方式 C：从 GitHub 安装（单包仓库时）

```bash
dsh plugin --profile web add github:<owner>/<repo>#v0.2.0
```

> 本包当前在 FinKB 仓库的 `dsh-finkb/` 子目录里。npm 的 git 依赖不支持“仓库子目录”，
> 所以要么先 `npm publish`，要么把这个目录拆成独立仓库打 tag。

安装完成后重启 DSH（或按插件页提示重载）。

## 接口声明

| 项 | 值 |
|---|---|
| bundle 名 | `finkb-dsh` |
| MCP serverName | `finkb`（工具名 `mcp__finkb__<tool>`） |
| transport | `stdio` |
| command | `$FINKB_MCP_BIN`，未设置时 `finkb-mcp` |
| 单次工具超时 | 180,000 ms |
| 预设 id / 名称 / 排序 | `finkb` / `财报助手` / `30` |
| 设置页卡片 | 设置 → 左侧「FinKB」；同源接口 `POST /finkb-settings/api` |
| 预设组成 | persona（财报助手人格）、agent-instructions、time-context、skill-filesystem、tool-skill、tool-ask-user |
| 技能 | `finkb-qa`（工具路由 + 回答纪律 + 数据边界） |
| 数据目录 | `FINKB_HOME`（本包不读写，由 Python 服务端负责） |

14 个工具：

| 类别 | 工具 |
|---|---|
| 问答 | `ask`、`compare`、`query_metric`、`search_documents` |
| 本体 | `describe_ontology`、`traverse_relations` |
| 合成/审查 | `evidence`、`verify` |
| 本地库管理（写操作） | `init_library`、`library_status`、`list_documents`、`add_document`、`remove_document`、`rebuild_index` |

环境变量（由 DSH 宿主进程透传，但注意下面第 1 条）：

| 变量 | 作用 |
|---|---|
| `FINKB_HOME` | 知识库目录；不设时 pip 安装用 `~/.finkb` |
| `FINKB_MCP_BIN` | 覆盖 `finkb-mcp` 路径（开发时指向仓库根的 `.venv/bin/finkb-mcp`） |
| `FINKB_LLM_SYNTH` | 设为 `1` 时开启受约束合成（默认关闭） |

1. **密钥请写在 `$FINKB_HOME/config/.env`，不要只靠 shell 环境变量。** DSH 启动 MCP 子进程时会清洗继承环境，名字含 `KEY`/`PASSWORD`/`SECRET`/`TOKEN` 的变量（如 `LLM_API_KEY`）会被丢掉；FinKB 会读 `$FINKB_HOME/config/.env`，所以写在文件里最稳。
2. `FINKB_HOME`、`FINKB_MCP_BIN`、`FINKB_LLM_SYNTH` 不匹配清洗规则，可以放在 shell 环境或 DSH 启动环境里。

## 设置页（密钥卡片）

安装后打开 **设置 → 左侧「FinKB」**：

| 字段 | 写入 `.env` 的键 | 说明 |
|---|---|---|
| LLM API Key | `LLM_API_KEY` | 必填；事实抽取、合成、视觉解读 |
| LLM Base URL | `LLM_BASE_URL` | 留空用默认值 |
| LLM 模型 | `LLM_MODEL` | 留空用默认 `deepseek-v4.1-flash` |
| SiliconFlow API Key | `SILICONFLOW_API_KEY` | 可选，bge-m3 向量检索 |
| MinerU Token | `MINERU_API_TOKEN` | 可选，导入新 PDF 时的高质量解析 |

- **热加载**：保存后不需要重启 DSH 或 MCP 进程。FinKB 每次调用都会重新读取 `$FINKB_HOME/config/.env`，下一次工具调用就生效。
- **知识库目录不在这里改**：目录由部署环境变量 `FINKB_HOME` 决定（MCP 子进程启动时固定），运行期修改需要重启 DSH。卡片上只显示当前目录，避免“保存成功却没生效”的错位。
- **留空 = 保持不变**：密钥输入框留空不会清掉已配置的值；输入框占位符显示“已配置/未配置”。
- **不回传明文**：状态接口只返回“是否已配置”和 Base URL/模型，不返回密钥原文。
- **为什么不在 shell 里配**：DSH 启动 MCP 子进程时会清洗名字含 `KEY`/`TOKEN`/`SECRET` 的环境变量，所以密钥写文件最稳。

## 装好后怎么验证

1. 插件页能看到 `finkb-dsh`，且已启用；
2. 会话工具列表里出现 `mcp__finkb__ask` 等 14 个工具；
3. 新建会话时预设选择器出现「财报助手」；
4. 设置页左侧出现「FinKB」，填好 LLM API Key 后显示“已保存”；
5. 试问：

   ```
   比亚迪 2025 年营业收入是多少？
   美的集团 2023 到 2025 年的营业收入对比
   比亚迪 2025 年报里存货附注在第几页？
   ```

   期望：数字带单位与 `[Ref: 文档#P页码]`；对比题返回逐期数值；知识库没有的公司会明确说“不在语料范围”。

## 常见问题

- **设置页没有「FinKB」**：确认 `finkb-dsh` 已启用且 DSH 是 Web 形态（纯对话部署没有 `webServer`，不挂设置接口）；看 DSH 日志里 `finkb-settings` 是否有报错。
- **想换知识库目录**：设置页不提供修改；设置环境变量 `FINKB_HOME` 后重启 DSH，卡片会显示新目录。
- **保存提示失败**：检查 `$DSH_HOME` 可写、知识库目录可写；接口只接受本机同源请求，远程访问时需要在 DSH 的 trusted hosts 里放行。
- **工具没出现**：说明 `finkb-mcp` 没启动。先单独运行 `finkb-mcp`（或 `FINKB_MCP_BIN=...`）看报错；DSH 日志里会有 MCP 连接失败信息。本包故意设 `failOnStartupError: false`，不阻断 DSH 启动。
- **工具报“知识库为空”**：`FINKB_HOME` 没设或没建库，按前置条件初始化并导入年报。
- **问答很慢**：开启合成（`FINKB_LLM_SYNTH=1`）时每次最多两次 LLM 调用；关掉后走抽取式，秒级返回。
- **想卸载**：插件页停用/卸载，或 `dsh plugin --profile web remove finkb-dsh`；数据仍在 `FINKB_HOME`，不会被删。
- **改了包名/装在别的名字下**：`presets/finkb.patch.yml` 里的技能路径用 `createRequire(baseUrl).resolve('finkb-dsh/package.json')` 解析，包名必须保持 `finkb-dsh`（或同步改这行表达式）。

## 设计边界

- 只做接线：MCP 注册 + 预设/技能，不实现检索、推理、解析。
- 只带一个设置页卡片，没有侧边栏面板；产品形态是「插件页条目 + 工具调用 + 预设选择器 + 设置页密钥卡片」。
- 不带数据：年报、事实库、向量索引都不进 npm 包。
- 升级方式：FinKB 升级只需重装 Python 包；本包只在接口变化（工具名/预设结构）时升版本。
