# Data Model

> ABI 的跨边界对象是 frozen Pydantic models；当前代码真相位于 `src/abi/types/`。本文说明模型族和
> durable 绑定，不把旧的 pass/TranslationUnit 结构当作当前协议。

## 1. 共同原则

1. 外部输入、LLM 输出、CLI 参数、文件内容和 ledger rows 在边界解析为强类型。
2. 模型默认 frozen；历史事实通过追加新版本/receipt 表达，不原地改写。
3. 参与 identity、fingerprint 或 receipt 的 JSON 使用 compact、sorted-key canonical serialization。
4. artifact、reason code、chapter stem 与 capability 使用小写 ASCII 命名空间。
5. checkpoint 中的框架对象不是业务模型，不能传入 PolicyEngine 或 completion predicate。

## 2. Book IR

`src/abi/types/book.py` 定义 ingest 后的统一结构：

- `BookMeta`：book ID、标题、作者、源语言、格式、路径、SHA-256、检测时间和 warnings；
- `Section`：稳定 section ID、层级、heading trail、paragraphs 与 children；
- `Paragraph`：稳定 paragraph ID、kind、原文、全书 position、section ID、anchors 与 attrs；
- `Book`：metadata、TOC、footnotes 与 references。

稳定 ID：

```text
book_id      = sha1(source_bytes)[:12]
section_id   = sha1(normalized heading trail)[:12]
paragraph_id = sha1(normalized text)[:10] + "-" + zero-padded position
```

章节调度最终使用 `source/toc.json` 中的小写 chapter stem；段落 ID 用于采样、定位与质量证据，不再作为
宏观 retry/checkpoint 单元。

## 3. Run 与配置

`src/abi/types/run.py` 的 `RunConfig` 包含：

- LLM endpoint、model、temperature、timeout、并发和 thinking mode；
- Langfuse host、key env 与 full/redacted payload；
- hard/warn cost cap；
- Planner horizon 与 rejection 上限；
- controller cycle、并发 Action、默认 attempt 与 semantic repair 上限；
- TOC refinement 开关。

CLI/config/env 合并后冻结一次。Action 授权与 retry 必须引用该 run 的冻结参数，不能因 resume 时配置漂移
而重解释旧 attempt。

## 4. 编排模型

`src/abi/types/orchestration.py` 是受约束动态控制平面的纯类型边界。

### 4.1 Plan 与能力

- `ActionSpec`：capability、input schema、kind、prerequisites、effects、evidence、tools、skills、read/write
  sets、retry policy、validator、resource class 与 side-effect/probe 声明；
- `PlanPatch`：objective、未来 1–5 个 `ProposedAction`、显式 dependencies 与 rationale；
- `AuthorizedAction`：PolicyEngine 解析并冻结后的 caller-canonical 参数、access sets、manifest、retry
  fingerprint 和 idempotency identity。

Planner 只能产生提案，不能构造 durable Action 或修改状态。

### 4.2 工件

- `ExpectedArtifactManifest`：授权前按参数精确展开的 canonical path、media type、evidence role 与 metadata；
- `ArtifactBundle`：某 action/attempt staged outputs 的非空、有序 exact bundle；
- `ArtifactRef`：committed canonical path、checksum 与 producer Action；
- `GateDecision` / `GateEvidence`：validator identity/version、决定、bundle digest 和 ordered checksums。

canonical artifact key 必须是相对 POSIX path；每个 component 匹配 `[a-z0-9._-]+`，禁止 `.`、`..`、
反斜杠和 `state/staging` 命名空间。

### 4.3 结果

Action executor 返回 typed `ActionOutcomeEnvelope`，有效结果类别包括：

- `Succeeded`：只表示 executor 返回完整候选 bundle，尚未提交业务成功；
- `RetryableFailure`：带稳定 error code；
- `RepairRequired`：带 semantic/integrity class、source 与 reason；
- `PermanentFailure`；
- `Indeterminate`：可能发生外部副作用，只能由绑定 probe 解析；
- `Paused`：budget 或 HITL 等显式暂停。

结果首先写 immutable outcome receipt，再由 controller 确定性路由。

## 5. Durable 状态

业务状态只存在 `state/run.db`。核心实体：

```text
run
  ├── plan versions / rejections
  ├── actions
  │   └── attempts
  │       ├── outcome receipts
  │       ├── gate receipts
  │       └── promotion intents
  ├── committed artifacts / gate evidence
  ├── incidents / repair bindings / unblock resolutions
  ├── budget facts / outbox events
  └── HITL claims / starts / continuation receipts
```

`RunStatus` 只有 `RUNNING`、`PAUSED_BUDGET`、`PAUSED_HITL`、`BLOCKED`、`COMPLETED`、`CANCELLED`。
Action 具有独立 lifecycle，例如 `AUTHORIZED`、`RUNNING`、`RETRY_WAIT`、`REPAIR_REQUIRED`、
`INDETERMINATE` 与 `SUCCEEDED`；不能把 run status 当作 Action status。

## 6. Snapshot

Planner 和 PolicyEngine 读取 `RunSnapshot`，它是 ledger 与 committed artifact facts 的只读投影，包含：

- 当前 run/plan；
- Action 与 outputs-current 状态；
- artifact/gate evidence；
- open incidents、rejection reasons 与预算；
- 当前 eligibility 所需的书籍和配置身份。

snapshot 不能携带可变 repository、checkpoint cursor 或直接文件写句柄。

## 7. 兼容边界

v0.2 不迁移旧 `pipeline_state.json`、旧 28-state run、旧 `TranslationUnit`/pass manifest 或进行中的任务。
ledger schema 使用显式 version；未知或旧版本在建表和恢复前 fail closed，不做隐式 upgrade。
