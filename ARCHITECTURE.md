# ARCHITECTURE.md

> 本文件描述 **AI Book Interpreter (ABI)** 的整体架构、分层与依赖规则。
> 详细强制规则见 [`docs/DESIGN.md`](./docs/DESIGN.md)。

## 1. 高层视角

ABI 是一个**自包含的自主 agent**：它在进程内完成 `public-domain-books-translation`
原本交给外部 agent 客户端（Claude/Codex）做的事——按编号阶段提示 `00→19` 驱动一个
**28 态状态机**，调用工具，遇到硬质量门禁就自循环修复，直到产出版本化 EPUB。

```
abi make-book ──▶ Orchestrator（按 28 态状态机逐阶段推进）
                     │  载入当前阶段提示 + 工具子集
                     ▼
              Stage 运行：LLM tool-calling agent loop（有界）
                     │  agent 认为完成 ──▶ 确定性 validator 校验
                     │                         │ FAIL：带原因重试
                     ▼                         ▼ PASS：推进状态 + 记录门禁
              providers.llm / providers.agent_runtime
                     （Langfuse trace + events.jsonl + BudgetGate 成本上限）
```

宏观流程是**显式状态机 + 确定性门禁**（不是开放式 planner）；自主性体现在阶段内的
翻译、QA、修复、路由决策。状态持久化在 `state/pipeline_state.json`，崩溃后
`abi resume` 从断点继续。

## 2. 分层架构

```
types → config → ir → project → epub → qa → release → tools → stages → orchestrator → cli
providers (llm, agent_runtime, observability) ← 任何业务层（providers 不依赖业务）
```

| 层 | 职责 | 不得做 |
| --- | --- | --- |
| `types` | pydantic 模型、领域类型 | 任何 I/O |
| `config` | 配置解析、默认值 | 业务逻辑 |
| `ir` | 解析 epub/txt → `Book` IR + 章节切分 | 调用 LLM |
| `project` | 书籍工程目录合约 + 28 态 `PipelineState` 持久化 | 调用 LLM |
| `epub` | EPUB 构建、publication lint、资源检查、EPUBCheck | 调用 LLM |
| `qa` | 分层随机抽检采样器 + 卓越线 validator | 调用 LLM |
| `release` | 版本化发布 / 私人自用产物 | 调用 LLM |
| `tools` | 暴露给 agent 的工具带（沙箱文件、ingest、门禁、子 agent） | 业务决策 |
| `stages` | 各阶段 agent 调用 + 确定性 validator | 直接拼底层 SDK |
| `orchestrator` | 驱动 28 态状态机到 DONE | — |
| `cli` | typer 命令 → orchestrator | 业务逻辑 |
| `providers` | LLM router、agent 运行时（LangGraph）、可观测性 | 业务决策 |

**单向依赖**由 `docs/DESIGN.md` 规则 D1 定义。`langchain*` / `langgraph*` / `langfuse*`
等只能在 `providers/**` 出现（规则 D2）。所有 LLM 调用（含子 agent）都经
`providers.llm` 的 `LLMRouter` 或 `providers.agent_runtime` 的 `AgentRuntime`，因此自动
接入 Langfuse + `events.jsonl` + `BudgetGate`。

## 3. 书籍工程目录合约（运行时）

替代旧的 `runs/<book-id>/<run-id>/`。每本书一个工程根：

```
books/{target}/{NNNN}_{目标语言书名}/
├── source/            # 原文 raw + 清洗文本 + manifest + toc.json
├── metadata/          # book.yaml, rights_checklist, 研究, style_profile
├── references/        # 复制进来的质量门禁/标准/政策参考（agent 读取）
├── skills/            # expert-translation-quality / defect-families
├── chapters/{src,translated,final}/
├── glossary/          # terms.csv（locked/preferred/avoid + 禁用正文写法）+ style_guide
├── qa/                # pretranslation / chapter_controls / fidelity / ... / gates
├── preproduction/     # stage1 spec + stage2 样章 EPUB
├── reviews/           # 随机抽检轮次 + 独立双 agent 评审
├── output/            # book.epub + release/ + 各门禁 JSON 报告
├── retrospective/
└── state/             # pipeline_state.json + run.log
```

所有工件都是**人类与 agent 都可读**的，无不可解释二进制状态。

## 4. 翻译方法（核心）

抛弃滑动窗口，采用持久工件模型：

- 全局 + 本书研究 → `metadata/*research*.md` + `metadata/style_profile.md`。
- A/B/C/D 试译门禁（`PRETRANSLATION_PASS` 才能批量）。
- 持久 `glossary/terms.csv` + `glossary/style_guide.md`。
- **精简的每章翻译调用**：原文 + 最关键 5-8 条文体规则 + 仅命中的术语；只输出译文。
- 每章 `08a` 全量译后控制（零问题 PASS 硬门禁）后才进下一章。
- 忠实度 / 可读性+意象 / 术语三审 + 章节门禁 → `chapters/final/`。
- 任一可复现问题 → 问题族全书审计 + 技能回填。

## 5. 后 EPUB QA 与发布

- 预制作规格 + 样章 EPUB PASS 后才全书构建。
- Python `publication_lint` + `asset_manifest_check` + `epubcheck`（Java jar）门禁。
- 分层随机抽检：确定性采样器 + 两个独立评审子 agent + validator（avg≥92/min≥88、
  `release_confidence≥0.80`、≥2 连续 PASS 轮）。
- 版本化发布到 `output/release/`（私人自用模式到被忽略的 `output/private_artifacts/`）。

## 6. 可观测性与成本

每次 LLM 调用写入 `events.jsonl`，token/cost 聚合到 `metrics.json`，并经 Langfuse
trace；`BudgetGate` 在 `config.cost.hard_cap_usd` 触发时优雅停止（保留断点）。

## 7. 外部前置依赖

- OpenAI 兼容端点（`LLM_API_KEY` 等）。
- 可选 Langfuse keys。
- **EPUBCheck 需要 JRE**（`java` 在 PATH）。安装 `epub` extra 获取打包 jar，或设
  `ABI_EPUBCHECK_JAR`，或让 `epubcheck` 在 PATH。
