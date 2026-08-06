# AI Book Interpreter

> 一个**自包含的自主翻译 agent**：输入公版书 `txt` / `epub`，按编号阶段提示 `00→19`
> 驱动 28 态状态机，自循环过质量门禁，输出**版本化、经质量门禁的 EPUB 译本**。
> 这是 `public-domain-books-translation` 工作流的进程内独立 agent 化实现——不依赖外部
> agent 客户端运行。

本项目以 [OpenAI 工程技术：在智能体优先的世界中利用 Codex](https://openai.com/zh-Hans-CN/index/harness-engineering/) 为工程范式：**人类掌舵，智能体执行**。
代码仓库本身即"记录系统"——所有设计、规范、规则都被组织为智能体可读、可机械化执行的工件。

---

## 一句话定位

> 不是把 LLM 调用包一层 CLI，而是构建一个**可恢复、可观测、可强制不变量**的自主流水线，
> 让书籍翻译质量经研究 → 试译 → 逐章控制 → 多审 → 随机抽检收敛到可发布的 EPUB。

## 核心特性

| 特性 | 说明 |
| --- | --- |
| **自主 agent + 28 态状态机** | 阶段提示 `00→19` 驱动；遇硬门禁自循环修复，直到 `DONE` |
| **持久工件上下文** | 用 `glossary/terms.csv` + `metadata/style_profile.md` 替代滑动窗口 |
| **精简翻译调用** | 每章只喂原文 + 5-8 条文体规则 + 命中术语，只输出译文 |
| **确定性质量门禁** | 试译 PASS、每章零问题控制、章节门禁、出版 lint、EPUBCheck、分层随机抽检 |
| **完整 EPUB 产出** | Python 原生 MD→XHTML+OPF+nav+zip 构建；版本化发布 |
| **私人自用模式** | 本地源 + 私人自用声明 → 被忽略的 `output/private_artifacts/` |
| **可恢复** | 进度持久在 `state/pipeline_state.json`；`abi resume` 从断点继续 |
| **可观测 + 成本上限** | 所有 LLM 调用（含子 agent）经 Langfuse + `events.jsonl` + `BudgetGate` |
| **LangGraph tool-calling** | agent 运行时集中在 `providers/agent_runtime`，业务层不碰框架 |

## 文档地图

> 本文件只做入口。要深入任何主题，请按 [`AGENTS.md`](./AGENTS.md) 的索引继续阅读。

- [`AGENTS.md`](./AGENTS.md) — 智能体/贡献者的"地图"，约 100 行
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — 系统架构总览与分层依赖图
- [`docs/`](./docs) — 全部设计文档、产品规范、执行计划与质量/可靠性/安全规范

## 环境要求

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | **>= 3.11** | 使用 `StrEnum` / `match` 等新语法 |
| 包管理器 | `pip` 或 [`uv`](https://docs.astral.sh/uv/) | 推荐 `uv` |
| LLM 端点 | 任意 OpenAI 兼容 | OpenAI / DeepSeek / Together / 本地 Ollama / vLLM 均可 |
| **JRE（Java）** | 任意 | **EPUBCheck 门禁需要**；装 `epub` extra 取打包 jar，或设 `ABI_EPUBCHECK_JAR` |
| 可选：Langfuse | v2.x | 缺凭据时自动 no-op，不影响主流程 |

## 安装

```bash
git clone https://github.com/<your-org>/ai-book-interpreter.git
cd ai-book-interpreter

# 方式 A：uv（推荐）
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# 方式 B：纯 pip
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 验证安装
abi --help
```

> `-e ".[dev]"` 同时安装 `pytest`、`ruff`、`mypy` 等开发依赖；只跑翻译可去掉 `[dev]`。

## 配置

所有运行时配置走环境变量。推荐用 `.env` 文件：

```bash
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY；其余按需调整
```

最少必填的三项：

```bash
LLM_BASE_URL=https://api.openai.com/v1   # 或 DeepSeek / Ollama / vLLM 等
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
# 某些兼容端点的 thinking mode 不支持 agent tool_choice 时设置：disabled
ABI_LLM_THINKING=provider_default
```

可选项（Langfuse 追踪、滑动窗口大小、批量大小、并发度、`runs/` 目录位置等）的完整说明见 [`.env.example`](./.env.example) 与 [`docs/product-specs/cli-and-config.md`](./docs/product-specs/cli-and-config.md)。

## 启动引导

支持的输入：**EPUB / TXT**（本地路径或 URL）。

```bash
# 1) 制作一本书：英文公版 -> 简体中文 EPUB，跑到 DONE
abi make-book book.epub --source-target en-zh-Hans --title "书名"

# 2) 只跑到某个阶段（例如先看到全部章节翻译完）
abi make-book book.txt --source-target en-zh-Hans --until TRANSLATED

# 3) 切换到 DeepSeek 兼容端点 + 设成本上限
abi make-book book.epub -st ja-es --base-url https://api.deepseek.com/v1 \
    --model deepseek-chat --max-cost-usd 20

# 4) 私人自用（本地源，产物进被忽略的 output/private_artifacts/）
abi make-book ~/my.epub -st en-zh-Hans --mode private-use

# 5) 中断后从断点继续
abi resume books/zh-Hans/0001_书名

# 6) 查看某工程的状态机与门禁
abi state books/zh-Hans/0001_书名
```

工程默认落到 `books/{target}/{NNNN}_{目标语言书名}/`，包含完整目录合约、
`state/pipeline_state.json`、`events.jsonl`、各门禁 JSON 报告，以及
`output/release/` 下的版本化 EPUB。

## 工程理念（摘要）

1. **代码仓库即记录系统**：知识不存于 Slack 或脑中，而以 Markdown / 模式 / 可执行计划形式入库。
2. **渐进式披露**：`AGENTS.md` 是地图，不是百科全书。智能体按需深入。
3. **规范架构**：固定分层 + 单向依赖，由自定义 linter 机械执行。
4. **品味即不变式**：把"该这么做"编码成机器可校验的规则，而不是约定。
5. **吞吐量改变合并理念**：短生命周期 PR、非阻塞 CI、随时清理"AI 残渣"。

完整理念见 [`docs/design-docs/core-beliefs.md`](./docs/design-docs/core-beliefs.md)。

## 当前状态

- **版本**：v0.2 — 自包含 agentic EPUB 流水线（survey/translate/assemble 旧流程已移除）。
- 28 态状态机、阶段提示链、工具带、EPUB 构建/门禁、随机抽检、版本化发布均已落地。

## License

TBD
