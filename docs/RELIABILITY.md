# RELIABILITY.md

> ABI 的长任务恢复以 `state/run.db` 的 durable facts 为准。checkpoint、events 和文件时间戳都不能
> 单独证明业务成功。本文件定义当前重试、检查点、幂等、冲突与人工恢复语义。

## 1. 权威边界

| 数据 | 位置 | 权威性 |
| --- | --- | --- |
| run、plan、Action、attempt、receipt、gate、intent、incident、human decision | `state/run.db` | 唯一业务真相 |
| Action 消息、工具游标、interrupt | `state/graph_checkpoints.sqlite` | 仅恢复 provider runtime |
| attempt 输出 | `state/staging/{action_id}/{attempt}/` | promotion 输入；由 receipt/intent 绑定 |
| canonical 工件 | 书籍工程目录 | 只有 ledger checksum/receipt 绑定后才是 committed fact |
| events、metrics、status projection | `events.jsonl` 等 | 可重建投影，不授权 transition |

启动和 `resume` 总是先运行 Reconciler。零 run、多 run、foreign checkpoint、stale identity、缺失或冲突
receipt 都 fail closed；不得隐式创建第二个 run 或复活旧 attempt。

## 2. 执行与成功协议

每个 Action 在 dispatch 前冻结 parameters、expected manifest、evidence refs、retry policy/fingerprint，
并创建唯一 staging namespace。executor 返回后先写 immutable `attempt_outcome_receipts`，然后 controller
才可处理结果。

普通 `Succeeded` 的顺序不可缩短：

1. 校验 outcome 的 caller-canonical、非空、exact-manifest bundle；
2. staging-aware validator 产生绑定 validator identity/version、bundle digest、ordered checksums 的决定；
3. PASS gate receipt 与完整 promotion intent 集在一个 SQLite 事务中创建；
4. 每个 intent 以 create-only 语义提升并标记 `COMMITTED`；
5. 对完整 canonical bundle 做 unified postcheck；
6. 最后一个 ledger transaction 提交 artifacts、gate evidence、cost 与 Action success。

文件系统与 SQLite 不跨介质原子。任一步崩溃都由 Reconciler 按 durable intent/checksum 补完；identity、
checksum、目录安全或后验 drift 会把 intent 变为不可逆 `CONFLICT` 并阻断，而不是猜测成功。

Agent 报告完成但只写出 expected manifest 的真子集时，executor 在原 Action/attempt 和同一 create-only
writer 内最多启动一次有界的 manifest-completion loop。该 loop 保留原 Action task context，并追加只允许
写缺失路径的明确指令与缺失清单；已有 staged 输出保持不可改写。补齐后仍按完整 exact-manifest 走
validator。若补齐后仍缺失，才生成
`agent_incomplete_outputs`，交给 frozen Action retry policy 创建新 attempt。这样偶发漏写不会让已完成的
多章节工作整批重跑，同时也不会用 canonical 旧文件伪造本次 review evidence。

如果进程已经在 attempt 持久化为 `RUNNING` 后退出，但尚未写 outcome receipt，则不能事后把上述
completion loop 当作通用恢复器。Reconciler 只按 frozen manifest 精确扫描 staging：只有完整 bundle 可
重建 receipt；真子集或无法证明的结果必须产生 integrity incident 并 `BLOCKED`。这是当前单进程模型下
故意保留的 fail-closed 边界。

## 3. 结果分类与恢复矩阵

| Durable facts | 安全恢复 |
| --- | --- |
| `AUTHORIZED`，attempt 未开始 | 首次 claim，持久化 `RUNNING` 后 dispatch |
| `RUNNING`，outcome receipt 缺失 | 按 frozen manifest 精确扫描 staging；不能证明则 integrity block |
| outcome receipt 已有、未路由 | 读取 effective outcome，幂等执行确定性路由 |
| `RetryableFailure` 且 frozen policy 允许 | 旧 attempt/Action=`RETRY_WAIT`；显式创建 attempt+1、新 staging |
| retry 不允许或耗尽 | `PERMANENT_FAILED` 或按证据阻断；不能改 policy 后重放旧 attempt |
| mapped semantic repair | 旧 attempt/Action=`REPAIR_REQUIRED`，run 保持 `RUNNING`；新 plan/action/staging |
| integrity、未知分类或 conflict | 保留事实，run=`BLOCKED`；Planner 不自动 repair |
| `Indeterminate` 外部副作用 | 只授权绑定原 operation/idempotency/policy 的只读 probe |
| probe=`succeeded` | 原 Action 可按 immutable resolution 解析成功 |
| probe=`absent` 且 retry 合法 | 原 Action=`RETRY_WAIT`，创建 attempt+1 |
| probe=`unknown` | integrity incident + `BLOCKED` |
| gate/intents 已有、promotion 部分完成 | 对账并补完；不重跑 executor |
| ledger success、canonical drift | 原 success 不改写；新 integrity incident + `BLOCKED` |
| `PAUSED_BUDGET` | 调高预算并记录 evidence 后恢复 |
| `COMPLETED` / `CANCELLED` | terminal；resume/unblock/cancel 不得重开 |

## 4. HITL continuation

初始 `Paused(reason="hitl")` outcome receipt 永不改写。`approve` 验证 exact run/action/attempt/thread、
public interrupt ID、ordered decisions 与原 pause digest，在 ledger 中依次记录：

```text
CLAIMED → STARTED(stable resume_invocation_id) → RESOLVED(continuation sequence)
```

continuation 是 append-only receipt；effective-outcome API 选择最新 sequence。若 `STARTED` 后崩溃，
inspector 只能读取公共 checkpoint state：可证明 terminal/next pause 才追加 receipt，否则 Action/attempt
变为 integrity-class `INDETERMINATE`，run=`BLOCKED`。`Succeeded` continuation 仍走完整 gate/promotion/
postcheck/commit，不因人工批准而直接成功。

## 5. 人工 unblock

`unblock` 只接受：

- budget pause，且不携带 replacement/canonical resolution；或
- integrity block，带 source Action、reason、evidence refs，以及每个 conflict canonical path 的 exact
`removed` / `selected:sha256` 处置。integrity fact 可以来自 controller 的 `integrity_guard`，也可以来自
Dispatcher 边界已经写入 immutable outcome receipt 的 `action_outcome`；两者都必须由 Action、attempt、
reason code 与 open incident 精确绑定后才能人工替换。

integrity unblock 保留旧 Action、attempt、receipt、intent 与 conflict，创建 exactly one new plan version、
new Action ID 和 `state/staging/{new_action}/1`。request canonical JSON/digest 与 replacement identity 写入
`unblock_resolutions`。该 replacement 是已经人工确认的恢复工作，即使原 incident 的普通 eligibility
发生漂移也保持可调度；普通 operational replacement 仍必须重新通过当前 eligibility，不能越过 stale
上游产物。

常规 semantic repair 禁止借 manual unblock 绕过自动 policy-mapped replan。只有 `review.*` 的修复代数
达到上限并进入 `semantic_repair_stalled` 后，才允许带 source review Action、reason 与修复证据执行一次
route reset：它只重置“自上次证据修复以来”的 replacement 计数，不关闭真实质量 incident。缺陷消息通过
`semantic_repair_bindings` 绑定到 replacement Action，并直接进入 Action prompt；不依赖 incident 当时是否
仍在 snapshot 的 open 集合中。只有成功工件提交才能最终关闭该 semantic incident。

## 6. 预算、限流与可观测性

预算在 Planner、Action、agent turn 与工具调用前检查；hard cap 产生 `PAUSED_BUDGET`，不取消 durable
事实。provider 的瞬时网络/429 退避属于调用层；它不能替代 Action-level frozen retry policy。所有 LLM
调用经 providers 写 trace、token/cost/latency；outbox 事件在业务事务中创建，再幂等投影到本地事件流。
agent 内部允许并行 `read_file` / `grep`，但所有写入、构建、门禁和其他有副作用的工具调用都串行，
避免 OpenAI-compatible provider 对大型并行 tool-call 历史产生协议误判，并保持 mutation 顺序可审计；
Action 之间的并行仍由 Scheduler/Dispatcher 的 read/write set 与并发上限控制。

## 7. 故障演练要求

测试必须覆盖 outcome receipt、gate/intents、单个 promotion、unified postcheck、success transaction、
retry successor、semantic repair、probe resolution、HITL CLAIMED/STARTED/continuation 与 unblock 前后的
崩溃边界。恢复后不得重复 executor attempt ID、覆盖 canonical 工件、重置 conflict 或把 projection 当真相。
