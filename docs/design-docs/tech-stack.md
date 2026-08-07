# Tech Stack（当前）

> 本文件记录 v0.2 受约束动态编排实现的技术选型。精确版本以 `pyproject.toml` 和 `uv.lock` 为准；
> 业务协议以 [`dynamic-agent-orchestration.md`](./dynamic-agent-orchestration.md) 为准。

## 概览

| 关注点 | 当前选型 | 边界 |
| --- | --- | --- |
| 宏观编排 | LangGraph v1 + 定制 controller | LangGraph 保存运行游标；RunLedger 保存业务真相 |
| Action agent | LangChain v1 `create_agent` | 只在 `providers/agent_runtime` 使用，工具和路径按 Action 授权 |
| 结构化 LLM | LangChain / `langchain-openai` | Planner 与结构化调用统一经过 `providers.llm.LLMRouter` |
| LLM 协议 | OpenAI-compatible Chat Completions | `base_url`、key 与 model 可配置 |
| 业务持久化 | SQLite + `aiosqlite` | `state/run.db`，append-oriented durable facts |
| 运行时 checkpoint | LangGraph SQLite checkpointer | 宏观与 Action/HITL 使用独立数据库和 ownership marker |
| 观测 | Langfuse v4 + `events.jsonl` | Langfuse 管模型链路；本地事件是可重建投影 |
| 数据边界 | Pydantic v2 frozen models | 外部输入、模型输出、文件与 CLI 边界全部解析 |
| EPUB | ebooklib、lxml、自有 builder、EPUBCheck | 构建、出版 lint、asset manifest 与外部规范门禁 |
| CLI | Typer + Rich | lifecycle 命令只调用公开的强类型服务接口 |

## 1. 为什么是定制控制平面 + LangGraph

ABI 的业务状态包含 plan、Action、attempt、receipt、gate、promotion intent、incident、预算和人工决定。
这些事实需要精确的事务、幂等与审计语义，不能由通用 agent 消息历史推断。因此：

- `providers/orchestration_runtime` 提供与领域无关的 durable tick loop；
- `orchestrator/controller.py` 负责 snapshot、plan、authorize、dispatch、route、commit；
- `planning/PolicyEngine` 确定性计算 eligibility 并复核 Planner 的 `PlanPatch`；
- `project/RunLedger` 是唯一业务真相；
- LangGraph checkpoint 只恢复 graph cursor，不参与 completion 判定。

这个组合保留动态规划和断点恢复，同时避免让模型拥有状态写权限。

## 2. 为什么用 LangChain Action harness

开放式工作仍需要 tool-calling loop，例如本书研究、翻译、审校与复盘。ABI 使用 LangChain v1
`create_agent`，但在 provider 层之外不暴露框架对象：

- ActionRegistry 冻结 skills、tool allowlist、read/write sets 与 expected manifest；
- filesystem tools 使用 attempt-scoped create-only writer；
- 读取可并行，写入、构建、门禁和其他副作用串行；
- provider 不支持并行 tool calls 时显式设置 `parallel_tool_calls=False`；
- 每章 review 使用隔离的 agent loop，避免跨章消息和 tool history 污染；
- HITL interrupt 通过 durable public ID 与 append-only continuation 恢复。

业务模块不得直接 import `langchain*`、`langgraph*` 或 `langfuse*`，该约束由 linter 强制。

## 3. OpenAI-compatible 接入

统一配置为：

```text
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=...
LLM_MODEL=gpt-4o-mini
ABI_LLM_THINKING=provider_default
```

OpenAI、DeepSeek、兼容网关、本地 Ollama/vLLM 可通过同一边界接入。兼容并不意味着行为完全一致；
tool choice、thinking mode、content filter、structured output 和并行 tool-call history 都可能不同。
兼容处理集中在 providers：

- 结构化输出在 Pydantic 边界解析，非法响应不会进入业务层；
- provider 瞬时错误使用调用层的有界退避；
- content-filter 类错误只在可判定为瞬时/可改写时进入有界重试；
- Action-level retry 仍由冻结的 policy 决定，不能被 SDK retry 替代。

## 4. Langfuse v4 观测

`providers/observability/langfuse_client.py` 创建共享 Langfuse client；每次 LangChain invocation 创建独立
`CallbackHandler`，防止并行 Action 共享 handler 状态。`propagate_attributes` 把以下身份传播到 trace root：

- `run_id` → session；
- 稳定的 planner/Action trace name；
- capability、action ID、attempt、book 与 chapter metadata/tags。

payload 由单一开关控制：

| `LANGFUSE_FULL_PAYLOAD` | 行为 |
| --- | --- |
| `1` | 上传完整 prompt、messages 与 completion，适合受控调试环境 |
| `0`（默认） | 递归 mask 字符串正文，保留 role/type、token、模型、时延和结构 metadata |

缺少凭据、认证失败或服务不可达时，Langfuse provider 安全降级；业务 run、RunLedger 和本地
`events.jsonl` 不依赖远端观测成功。进程结束前调用共享 client 的 `flush()`，降低短任务 trace 丢失风险。

## 5. 持久化分层

| 文件 | 内容 | 是否可判定业务成功 |
| --- | --- | --- |
| `state/run.db` | run、plan、Action、attempt、receipt、gate、intent、incident、budget、HITL | 是，唯一权威 |
| `state/graph_checkpoints.sqlite` | 宏观 LangGraph cursor | 否 |
| `state/action_checkpoints.sqlite` | Action agent messages、tool cursor、interrupt | 否 |
| `state/staging/...` | 当前 attempt 的候选输出 | 否，必须通过完整 commit 协议 |
| canonical 文件 | 已提升工件 | 只有与 ledger checksum/receipt 绑定后才算 committed |
| `events.jsonl` / `metrics.json` | 可观测投影 | 否，可从 durable facts 重建 |

SQLite schema 使用显式版本与 ownership marker；零 run、多 run、foreign checkpoint、identity drift 或
checksum 冲突都 fail closed。

## 6. 主要依赖范围

当前 `pyproject.toml` 的关键范围：

```toml
aiosqlite = ">=0.20,<1"
pydantic = ">=2.7,<3"
langchain = ">=1.3.14,<2"
langchain-openai = ">=1.3.5,<2"
langchain-core = ">=1.3,<2"
langgraph = ">=1.2.9,<2"
langgraph-checkpoint-sqlite = ">=3.1,<4"
langfuse = ">=4.14.1,<5"
```

不要从本文复制精确锁定版本；使用 `uv sync` 与已提交的 `uv.lock` 复现开发环境。

## 7. 被拒绝的替代方案

- **固定阶段链：** 易预测，但无法根据缺陷做精确修复，也把恢复粒度放得过大。
- **通用 agent TodoList 直接做业务计划：** 无法表达 ABI 的 evidence、manifest、gate、write-set 与事务。
- **checkpoint 作为业务数据库：** provider schema 与消息历史不能证明工件提交。
- **业务层直接调用 SDK：** 会绕开预算、追踪、错误分类与 provider 兼容边界。
- **第一版引入外部 durable engine：** ABI 当前是单进程、本地文件密集型 CLI；复杂度收益不足。

## 8. 相关文档

- [LangGraph 与 ABI 动态状态机](./langgraph-and-state-machine.md)
- [ABI Agentic Pipeline](./agentic-pipeline.md)
- [可靠性协议](../RELIABILITY.md)
- [安全边界](../SECURITY.md)
