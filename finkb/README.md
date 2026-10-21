# FinKB — Phase 0（数据地基）

A 股财报知识库，架构见 [`../FinKB-方案v2.md`](../FinKB-方案v2.md)（v2.1）。

> 📓 **接手项目请先读 [`DEVLOG.md`](./DEVLOG.md)** —— 那里记录当前状态、设计决策、
> 踩过的坑与未验证项。本 README 只讲「怎么用」，DEVLOG 讲「为什么这样」。
> 工作约定见 [`AGENTS.md`](./AGENTS.md)。

当前实现范围：**Phase 0 — L0 凭证层**
（解析 → 页码锚定 → 页眉页脚清洗 → 跨页表格缝合 → StitchedTable）。

## 数据格式分层（重要）

清洗与缝合**不直接操作 MinerU 原生格式**，而是操作本项目自有的中间表示（IR）。
三层职责必须区分清楚：

```
MinerU content_list.json          外部格式，字段随版本变化
        │  content_list_to_pages()      ← 唯一的格式适配层
        ▼
Page / Block / TableBlock         本项目 IR（层内唯一真相）
        │  strip_running_noise()        ← 清洗只看 IR 的块顺序与行
        ▼
StitchedTable                     缝合产物，交给 L1
```

| 环节 | 输入格式 | 说明 |
|---|---|---|
| `content_list_to_pages` | MinerU `content_list.json` | **唯一**依赖 MinerU 字段的函数 |
| `html_table_to_rows` | MinerU `table_body`（HTML） | 用 `<th>` 存在性判断有无表头 |
| `strip_running_noise` | 本项目 IR | 与数据来源无关；pypdf 与 MinerU 路径共用 |
| `stitch_tables` | 本项目 IR | 同上 |

这样做的代价与收益：换解析器只需重写适配层；但**清洗逻辑的充分性依赖 IR 的块粒度**。
同一个页眉，在 pypdf 输出里是「块内的行」，在 MinerU 输出里是「独立的块」，
两者的清洗路径不同，必须分别验证（见 `tests/test_mineru_noise_path.py`）。

> mineru pipeline 后端本身会把页眉页脚丢进 `layout.json` 的 `discarded_blocks`，
> 走 MinerU 主路径时页眉大概率不进 `content_list`。本模块的清洗因此是**第二道保险**，
> 在 pypdf 兜底路径上是**唯一防线**。

## 目录

```
finkb/
├── src/finkb/
│   ├── common/models.py            # Page / Block / TableBlock / StitchedTable
│   └── parser/
│       ├── markdown_blocks.py      # 页码锚定 Markdown 解析；HTML 表格 → 行/网格
│       ├── noise_filter.py         # 运行页眉/页脚与残留页码清洗
│       ├── table_stitcher.py       # ★ 跨页表格缝合
│       ├── pipeline.py             # 解析 → 清洗 → 缝合 端到端入口
│       └── mineru_client.py        # MinerU 客户端 + content_list 适配 + pypdf 兜底
├── tests/                          # 74 项测试，全部离线可跑
└── data/{raw,parsed,parent_store}/
```

## 运行测试

```bash
cd finkb
../.venv/bin/python -m pytest -q
```

无需网络、无需任何 API key。

## 安装

```bash
cd finkb
../.venv/bin/pip install --no-build-isolation -e .
```

安装后可用 `finkb` 命令（也可用 `../.venv/bin/python -m finkb.cli`，需 `PYTHONPATH=src`）。

## 配置 MinerU（真实解析需要）

```bash
cd finkb
cp config/.env.example config/.env
# 编辑 config/.env，填入 MINERU_API_TOKEN
```

Token 从 <https://mineru.net/apiManage/token> 获取。
`config/.env` 已被 `.gitignore` 覆盖，不会提交；`config/.env.example` 会提交。

支持的变量：

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MINERU_API_TOKEN` | ✅ | — | Bearer token |
| `MINERU_BASE_URL` | | `https://mineru.net` | API 根地址 |
| `MINERU_MODEL_VERSION` | | `pipeline` | `pipeline` / `vlm` / `MinerU-HTML` |
| `MINERU_LANGUAGE` | | `ch` | PaddleOCR 语言码，中文是 `ch` 不是 `zh` |
| `MINERU_POLL_INTERVAL` | | `3` | 轮询间隔（秒） |
| `MINERU_POLL_TIMEOUT` | | `900` | 轮询总超时（秒） |

临时覆盖（不写文件）：

```bash
MINERU_API_TOKEN=xxx ../.venv/bin/python -m finkb.cli parse ...
```

## 命令行用法

```bash
# ① 先做小范围联调（强烈建议）：只解析 P43-P44，验证真实跨页表
finkb parse "../raw/002594_2025_年报.pdf" --pages 43-44

# ② 全量解析（268 页，消耗较多额度）
finkb parse "../raw/002594_2025_年报.pdf"

# ③ 离线模式：不调用 MinerU，用 pypdf 验证链路是否通
finkb parse "../raw/002594_2025_年报.pdf" --offline
```

产物落在 `data/parsed/<doc_id>/`：

| 文件 | 内容 |
|---|---|
| `page_anchored.md` | 页码锚定 Markdown（L0 落盘格式） |
| `content_list.json` | MinerU 原始输出（在线模式才有） |
| `tables/T001.md` | 每张逻辑表一个文件，便于人工核对 |
| `report.json` | 统计、噪音签名、缝合审计记录 |

退出码：`0` 成功 / `2` PDF 不存在 / `3` 未配置 token / `4` MinerU 调用失败。

## 流水线顺序

```python
from finkb.parser.pipeline import build_tables

result = build_tables(markdown, doc_id="002594_2025_年报")
result.summary()        # 页数=268 逻辑表=N 跨页缝合=M 剥离噪音行=534
result.tables           # 缝合后的逻辑表
```

顺序**强制**为「先清洗、后缝合」：缝合判据依赖「页首/页尾是什么块」，
未清洗的页眉会让判据失效。

## 关键设计

### 页码约定

`DocMeta.page` 一律为 **1-based 物理页码**，与 PDF 阅读器 `#page=N` 及方案 §9.3 的
`[Ref: {doc_id}#P{N}]` 锚点一致。MinerU 的 `page_idx` 是 0-based，在
`content_list_to_pages()` 里统一 +1。

### 跨页缝合判据

四个条件同时成立才缝合：

1. 第 N 页的表格是**该页最后一个内容块**；
2. 第 N+1 页的表格是**该页第一个内容块**；
3. 两表**列数一致**；
4. 第 N+1 页表格**无表头**，或其表头与第 N 页**完全重复**（重复表头会被丢弃）。

条件 4 是防误缝的关键：正常起始的表格几乎总带表头，无表头的页首表格绝大多数是
上一页的延续。三页及以上链式拆分由循环续接处理。

**「内容块」指非噪音块** —— 缝合会跳过 `Block.is_noise=True` 的块。这让缝合
不依赖清洗是否彻底，是比「先洗干净再缝」更稳的组合。

```python
result.tables[0].parent_id      # 002594_2025_年报#P43-P44_T01
result.tables[0].to_markdown()  # 还原成完整表，供 L1 抽取
result.stitch.records           # 审计记录（页码序列 + 缝合理由）
```

### 页眉页脚清洗

不靠固定文案，靠**跨页重复**这一统计特征：把候选行的数字替换成 `#` 得到签名，
签名在超过阈值比例的页面上出现即判定为运行页眉/页脚。

三条护栏（都是踩坑后加的）：

| 护栏 | 原因 |
|---|---|
| **表格行永不参与判定** | 跨页表头每页都会重复，一旦被当成页眉清掉，缝合后就没有列名了 |
| **阈值硬下限 = 2** | 比例阈值在小文档上会退化为 1，导致只出现一次的正文行也被删 |
| **单块页去重候选行** | 整页只有一个文本块时，头尾取自同一块，会重复计数把阈值虚高 |

另处理一类残留：页眉是 `公司名 年份 报告全文  44`，签名只能删文字部分，
页码 `44` 会作为独立行留下，进而让缝合误判「页首第一个块是文本」。
因此做**页码行识别**（数字须等于物理页码才删），覆盖两种形态：

- 页码是长块里的一行 → 只在文本块头/尾 `scan_*_lines` 范围内生效；
- 页码是**独立块**（MinerU 常见）→ 整块只有一个非空行时单独立判定。

## 已验证

| 项 | 结果 |
|---|---|
| 测试 | **74 项通过**，离线，0.46s |
| 真实 PDF 页数 | `002594_2025_年报.pdf` → 268 页，页码 1..268 |
| 运行页眉识别 | 签名 `比亚迪股份有限公司#年年度报告全文` 命中 **268/268** 页 |
| 真实清洗效果 | 剥离 **534 行**噪音，覆盖 268 页 |
| 真实跨页表定位 | P43 页尾「境内外股票 / 02676 / 纳芯微」→ P44 页首「债券 / 019742」，同一张证券投资表，14 列 |
| 清洗后 P44 首块 | 直接以 `债券 019742 24 特国 01 …` 开头（页眉与页码均已剥离） |
| 三页链式缝合 | `#P42-P44_T01`，5 行，`to_markdown()` 正确还原 |
| MinerU 路径（构造数据） | 页眉独立块被识别；无 `<th>` 的续接表正确保持 `header=None` 并缝合 |

## 按 MinerU 实测规范做的三处修正

端点与字段对照第三方实测规范（pipeline 2.6.4，
[mineru_specification-v1.0](https://raw.githubusercontent.com/fufankeji/VibeCodingCourse_v1/refs/heads/main/Week3_Claude_Code_Engineering/Lesson06_Multimodal_RAG_System/GraphRAGAgent/docs/mineru_specification-v1.0.md)）：

| 问题 | 原实现 | 修正后 |
|---|---|---|
| 标题检测 | 用 `type in {title, heading}` | `type=text` 且 **`text_level` 字段存在** → 标题；正文无该字段 |
| 本地文件上传 | 把本地路径当公网 URL 提交 | `file-urls/batch` 取预签名 URL → **裸 PUT（无任何 header）** → `extract-results/batch` 轮询 |
| 表格表头 | HTML 一律把首行当表头 | 按 `<th>` 是否存在判断，续接表保持 `header=None` |

## 已知限制

- `html_table_to_markdown` 不展开 `rowspan` / `colspan`，合并表头会被压平。
- `OfflinePdfParser` 只做逐页纯文本，**不含表格结构，不可用于 L1 抽取**，
  仅用于链路验证与测试。
- MinerU 端点取自第三方实测规范，**本项目尚未真实联调**；`MineruConfig` 中所有
  路径均可覆盖，首次接入请用小文件验证。
- **真实年报的跨页表缝合尚未在 MinerU 输出上端到端验证**——目前 P43→P44 的结论
  来自 pypdf 文本层观察，表格结构仍需 MinerU 产出后才能跑通完整链路。

## 下一步（Phase 1）

1. `src/finkb/fact_engine/duckdb_manager.py` —— `metric_dict` / `facts` / `relations` /
   `unmatched_pool` + `facts_dedup` / `v_company_annual_wide` 视图
2. 两段式抽取（补丁 P1）：LLM 摘录 Observation → Python 反查原文 + 归一化
3. `tests/test_fact_extraction.py` —— ≥10 个关键数值与原文绝对一致，`exact` 匹配率 ≥80%
