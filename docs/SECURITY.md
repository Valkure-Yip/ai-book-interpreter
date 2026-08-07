# SECURITY.md

## 1. 凭据

ABI 使用：

- `LLM_API_KEY`（或 `llm.api_key_env` 指定的变量）；
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`（或 `langfuse.*_key_env` 指定的变量）。

规则：

- 不接受 CLI `--api-key`，避免 key 进入 shell history；
- `.env` 只用于加载到进程环境，不应提交；
- LLM key 缺失时在创建 provider 前失败；Langfuse key 缺失时安全 no-op；
- provider exception 写入 outcome/events 前必须分类与脱敏，不能复制原始 response body 或 key；
- key 不得写入书籍工程、RunLedger、checkpoint、events、metrics、Langfuse metadata 或发布物。

## 2. 原书与译文

ABI 是本地 CLI，不运行项目方服务端；但配置的 LLM provider 会收到完成任务所需的原文、研究或译文
片段。使用云端 endpoint 前，用户必须自行确认版权、保密和 provider 数据保留政策。需要完全离线时，
使用本地 OpenAI-compatible endpoint。

以下本地内容都应按原书同等敏感度保管：

- `source/`、`chapters/`、`metadata/`、`glossary/`；
- `qa/`、`reviews/`、`preproduction/` 与 EPUB；
- `state/run.db`、两个 checkpoint DB 和 attempt staging；
- `events.jsonl` / `metrics.json` 中的身份、错误分类和用量 metadata。

private-use 模式把工程放到 `{books_root}/private/`，并为 source、private artifacts、events/metrics 写入本地
`.gitignore`。它不改变 LLM provider 数据流，也不自动把整个工程加密。

## 3. Langfuse payload

配置键是 `langfuse.upload_full_payload`，环境变量是 `LANGFUSE_FULL_PAYLOAD`：

| 值 | 行为 |
| --- | --- |
| `0` / `false`（默认） | v4 client 的 mask 递归把字符串 payload 替换为 `[REDACTED: ...]`，保留消息结构、数字、token、模型与时延 |
| `1` / `true` | 上传完整 prompt、messages 与 completion，仅应在明确授权的调试环境开启 |

每次 invocation 使用独立 CallbackHandler；共享 client 在 run 结束时 flush。Langfuse 状态和
`full_payload` 标志写入本地 `observability.langfuse` 事件，便于审计实际模式。缺 key、认证失败、import
失败或初始化失败时 handler 为 no-op；远端观测不能阻断或证明业务成功。

## 4. Prompt injection

书中出现的命令、提示词或“忽略之前指令”等文本都属于翻译对象，不是 agent 指令。防线包括：

- system/task prompt 明确区分 source 与控制指令；
- Action 只加载 Registry 允许的 skills 与 tools；
- 原文不能调用 `set_state`、`record_gate` 或任意 shell/network 工具；
- 写路径由 frozen expected manifest 和 attempt-scoped writer 限制；
- validator 不相信模型自报 PASS，而是重算结构与质量证据。

## 5. 文件系统

- canonical artifact key 是小写 ASCII 相对 POSIX path；禁止绝对路径、反斜杠、NUL、`.`、`..` 和
  `state/staging` 命名空间；
- permissioned reads 使用 pinned directory fd 与 `O_NOFOLLOW`，拒绝任一 symlink component；
- Dispatcher 在执行前重新展开 access sets，并与 durable authorization 精确比较；drift 属于 integrity
  failure；
- writer 只能在 `state/staging/{action_id}/{attempt}/` create-only 写入，不能覆盖 staged 或 canonical
  文件；
- EPUB parser 使用权限为 `0600` 的受控临时副本，并在成功/异常退出时删除；
- promotion 使用 checksum、intent 与 canonical postcheck；冲突 fail closed。

## 6. 网络与 provider 边界

业务模块不得直接 import 或调用 LLM/Langfuse SDK。所有模型流量通过 `providers.llm` 或
`providers.agent_runtime`，统一经过：

- BudgetGate；
- Langfuse/local event observability；
- provider error classification 与 secret-safe reporting；
- timeout、瞬时错误和 content-filter 的有界处理。

TLS 校验保持 SDK 默认开启；不要在实现中加入 `verify=False`。远端 URL 输入与 provider endpoint 都应视为
外部不可信边界。

## 7. 持久化与人工恢复

checkpoint、events、文件 mtime 和模型输出都不能授权业务 transition。只有 RunLedger typed facts、绑定的
checksums、gate receipts 与 promotion intents 可以证明成功。identity/checksum/receipt/canonical 冲突进入
immutable incident 与 `BLOCKED`；人工 `unblock` 必须覆盖每个冲突路径并提供 evidence ref，且只能创建
new plan/action/staging，不能改写旧事实。

## 8. 依赖与披露

直接依赖范围在 `pyproject.toml`，精确解析版本在 `uv.lock`。LangChain、LangGraph、Langfuse、Pydantic、
ebooklib、lxml 和 EPUBCheck 升级必须跑安全边界与恢复回归。正式公开漏洞披露渠道尚未建立；开源前需要
新增独立 security contact。

## 9. 不变量

1. secret 不进入 durable facts、事件、trace metadata 或工件。
2. 默认不把正文上传到 Langfuse；开启完整 payload 必须是显式选择。
3. 原文只能影响被授权的候选输出，不能改变业务状态或扩大工具权限。
4. 无法证明执行或提交结果时 fail closed，不用重试或人工口头确认掩盖 integrity failure。
