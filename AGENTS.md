# AGENTS.md

> **这是一张地图，不是一本说明书。**
>
> 你（不论是人类工程师还是 Codex/其他智能体）在动手前请用 30 秒读完本文，然后跳到对应的真实信息源。
> 当本文与 `docs/` 下的具体文档冲突时，**以 `docs/` 为准**——本文件的唯一职责是把你导航到正确的页面。

## 项目一句话

一个**自包含的自主翻译 agent**：输入公版书（txt/epub），由受约束的
`Planner + Policy + durable action loop` 选择下一动作，自循环过确定性质量门禁，输出版本化 EPUB。
（这是 `public-domain-books-translation` 工作流的进程内独立 agent 化实现。）

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
| 看强制不变量与分层规则 | [`docs/DESIGN.md`](./docs/DESIGN.md) |
| 理解动态控制循环与 durable state | `src/abi/orchestrator/controller.py` + `src/abi/project/run_ledger.py` |
| 看 Action 注册、权限与执行合约 | `src/abi/actions/` + `src/abi/planning/` |
| 看 agent 工具带 | `src/abi/tools/` |
| 看 agent 运行时（LangGraph） | `src/abi/providers/agent_runtime/` |
| 理解 LangGraph runtime 与 RunLedger/checkpoint 分层 | [`docs/design-docs/langgraph-and-state-machine.md`](./docs/design-docs/langgraph-and-state-machine.md) |
| 看下一代受约束动态编排设计（Planner + Policy + durable loop） | [`docs/design-docs/dynamic-agent-orchestration.md`](./docs/design-docs/dynamic-agent-orchestration.md) |
| 看 EPUB 构建 / 门禁 | `src/abi/epub/` |
| 看随机抽检 / 卓越线 | `src/abi/qa/` + `src/abi/assets/references/stratified_random_spotcheck.md` |
| 看版本化发布 / 私人自用 | `src/abi/release/` + `src/abi/assets/references/release_versioning.md` |
| 改 CLI 命令或参数 | `src/abi/cli/main.py` |
| 处理重试、限流、断点 | [`docs/RELIABILITY.md`](./docs/RELIABILITY.md) + `state/run.db` |
| 看 eval 标准 / 该衡量哪些指标 | [`docs/design-docs/eval-standard.md`](./docs/design-docs/eval-standard.md)（L1 流程可信度 / L2 中间产物+逐章译文 / L3 最终产物） |
| 现在该干什么？ | [`docs/exec-plans/active/`](./docs/exec-plans/active/) |

## 核心不变量（违反就是 bug，由 linter 强制）

1. **分层依赖单向**：`types → config → ir → project → epub → qa → release → tools → actions → planning → orchestrator → cli`，横切只能走 `providers`。详见 [`docs/DESIGN.md`](./docs/DESIGN.md)。
2. **边界处解析数据形状**：所有外部输入（LLM 响应、文件、CLI 参数）必须用 `pydantic` 在边界处解析为强类型，不允许 dict 透传。
3. **RunLedger 是唯一业务真相**：run/plan/Action/attempt/receipt/gate/intent/incident 只写 `state/run.db`；checkpoint 与投影不授权 transition，agent 不得自行宣布 PASS。
4. **LLM 调用必须可观测**：所有 LLM 调用（含子 agent）走 `providers.llm` 的 `LLMRouter` 或 `providers.agent_runtime` 的 `AgentRuntime`，自动接入 Langfuse trace + `events.jsonl` + `BudgetGate`。业务层禁止直接 import `langchain*` / `langgraph*` / `langfuse*`。
5. **结构化日志**：禁止 `print` / 裸 `logging.info(str)`；事件走 `events.jsonl`。
6. **翻译调用精简**：每章翻译只喂原文 + 5-8 条文体规则 + 命中术语，只输出译文；不混入 QA / EPUB / release 规则。
7. **不写回模板/原文**：`src/abi/assets/` 是模板源；具体书籍产物只写入书籍工程目录。

## 工作风格

- **深度优先拆解**：先把目标拆成最小独立单元（设计 → 类型 → 实现 → 测试 → 文档），逐个完成。
- **不要直接编辑生成物**：`docs/generated/` 与 `runs/` 都由代码生成；改的是上游。
- **PR 短生命周期**：尽量小，CI 不阻塞合并，依赖后续 PR 修正而非长期挂起。
- **文档先行**：增加任何能力前，先在 `docs/design-docs/` 加一份设计说明，并在本 `AGENTS.md` 表格补一行入口。

## 我卡住了

- 先看 `docs/` 是否已有答案。
- 没有的话：在 `docs/exec-plans/active/` 新增一份执行计划，说明卡点与候选方案。
- 仍无法决策才升级到人类。
