# AGENTS.md

> **这是一张地图，不是一本说明书。**
>
> 你（不论是人类工程师还是 Codex/其他智能体）在动手前请用 30 秒读完本文，然后跳到对应的真实信息源。
> 当本文与 `docs/` 下的具体文档冲突时，**以 `docs/` 为准**——本文件的唯一职责是把你导航到正确的页面。

## 项目一句话

输入学术书籍（txt/epub/pdf），输出 Markdown 译本 + 可选的章节摘要、思维导图。三遍流水线：**survey → translate → assemble**。

## 仓库布局

```
ai-book-interpreter/
├── AGENTS.md              ← 你正在读
├── ARCHITECTURE.md        ← 总体架构 + 分层依赖
├── README.md
├── docs/
│   ├── DESIGN.md          ← 架构不变量与 linter 规则
│   ├── QUALITY_SCORE.md   ← 翻译质量评分体系
│   ├── RELIABILITY.md     ← 重试 / 检查点 / 幂等
│   ├── SECURITY.md        ← API key、PII、版权
│   ├── design-docs/       ← 详细设计（流水线、滑动窗口、数据模型…）
│   │   └── index.md
│   ├── product-specs/     ← 用户可见行为（CLI、IO 格式、配置）
│   │   └── index.md
│   ├── exec-plans/        ← 可执行计划（active / completed）
│   ├── references/        ← 外部参考资料的 llms.txt 镜像
│   └── generated/         ← 由代码生成、勿手改
└── src/                   ← 源代码（按 ARCHITECTURE.md 分层）
```

## 你最常需要的入口

| 我想…… | 去看…… |
| --- | --- |
| 理解整体架构与分层 | [`ARCHITECTURE.md`](./ARCHITECTURE.md) |
| 看 v0.1 用了什么框架/库（LangChain、Langfuse、OpenAI-compatible） | [`docs/design-docs/tech-stack.md`](./docs/design-docs/tech-stack.md) |
| 理解三遍流水线 | [`docs/design-docs/pipeline.md`](./docs/design-docs/pipeline.md) |
| 理解滑动窗口上下文 | [`docs/design-docs/sliding-window.md`](./docs/design-docs/sliding-window.md) |
| 看数据结构（Book IR） | [`docs/design-docs/data-model.md`](./docs/design-docs/data-model.md) |
| 看翻译智能体如何工作 | [`docs/design-docs/agent-architecture.md`](./docs/design-docs/agent-architecture.md) |
| 加一种输入/输出格式 | [`docs/product-specs/io-formats.md`](./docs/product-specs/io-formats.md) |
| 接入新的 OpenAI 兼容端点 | [`docs/design-docs/tech-stack.md`](./docs/design-docs/tech-stack.md#2-为什么用-openai-compatible-统一接入) |
| 加一个非兼容的 LLM provider | [`docs/design-docs/agent-architecture.md`](./docs/design-docs/agent-architecture.md#provider-接口) |
| 改 CLI 命令或参数 | [`docs/product-specs/cli-and-config.md`](./docs/product-specs/cli-and-config.md) |
| 检查/调整质量评分逻辑 | [`docs/QUALITY_SCORE.md`](./docs/QUALITY_SCORE.md) |
| 处理重试、限流、断点 | [`docs/RELIABILITY.md`](./docs/RELIABILITY.md) |
| 现在该干什么？ | [`docs/exec-plans/active/`](./docs/exec-plans/active/) |

## 核心不变量（违反就是 bug，由 linter 强制）

1. **分层依赖单向**：`types → config → ir → survey → translate → assemble → runtime → cli`，横切只能走 `providers`。详见 [`docs/DESIGN.md`](./docs/DESIGN.md)。
2. **边界处解析数据形状**：所有外部输入（LLM 响应、文件、CLI 参数）必须用 `pydantic` 在边界处解析为强类型，不允许 dict 透传。
3. **段落 ID 稳定**：段落 ID = `sha1(normalize(content))[:12] + position_suffix`，**纯函数**，不随运行变化。
4. **LLM 调用必须可观测**：所有 LLM 调用走 `providers.llm.get_chat_model()`，自动接入 Langfuse trace + 本地 `events.jsonl`，自动累计 token/cost。业务层禁止直接构造 `ChatOpenAI` 或 import `langchain*` / `langfuse*`（lint 强制）。
5. **结构化日志**：禁止 `print` / 裸 `logging.info(str)`；必须 `log.event("name", **fields)`。
6. **段落-译文一一对应**：Pass 3 装配时，源 IR 中每个 `paragraph_id` 都必须有对应译文或显式的"跳过"记录。

## 工作风格

- **深度优先拆解**：先把目标拆成最小独立单元（设计 → 类型 → 实现 → 测试 → 文档），逐个完成。
- **不要直接编辑生成物**：`docs/generated/` 与 `runs/` 都由代码生成；改的是上游。
- **PR 短生命周期**：尽量小，CI 不阻塞合并，依赖后续 PR 修正而非长期挂起。
- **文档先行**：增加任何能力前，先在 `docs/design-docs/` 加一份设计说明，并在本 `AGENTS.md` 表格补一行入口。

## 我卡住了

- 先看 `docs/` 是否已有答案。
- 没有的话：在 `docs/exec-plans/active/` 新增一份执行计划，说明卡点与候选方案。
- 仍无法决策才升级到人类。
