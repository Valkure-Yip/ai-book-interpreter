# ABI Eval Standard

> ABI eval 分三个平面：L1 重放 durable policy facts，L2 衡量中间工件与逐章译文，L3 检查最终 EPUB、
> 随机抽检与 release。L1 不读取 checkpoint 私有表，也不把 events 或文件存在性当作业务真相。

## 1. 三平面

| 平面 | 对象 | 核心问题 | 权威输入 |
| --- | --- | --- | --- |
| L1 流程可信度 | 动态 Action/attempt/gate/recovery lineage | 是否不可绕过、可重放、fail closed？ | RunLedger typed public reads + canonical artifacts |
| L2 翻译质量 | 研究、术语、章节译文、QA 中间工件 | 译文是否完整、忠实、可读且术语一致？ | committed project artifacts |
| L3 成品质量 | EPUB、抽检、独立评审、release | 成品是否可发布并满足卓越线？ | committed EPUB/review/release evidence |

任一平面 FAIL，整体 FAIL。L1 PASS 不能补偿差译文，L2/L3 高分也不能补偿绕过门禁。

## 2. L1 gate integrity

每个 committed gate evidence 必须重放并同时绑定：

- Action 与 attempt 的 caller-canonical exact expected manifest/digest；
- immutable ordinary-success outcome receipt、canonical bundle JSON/digest 与 evidence refs；
- validator identity/version、canonical decision digest 与 ordered artifact checksums；
- gate receipt 同事务创建的完整 ordered promotion intents；
- 每个 intent=`COMMITTED`，且 unified canonical artifact postcheck 在 ledger success 前通过；
- post-success drift 只产生 immutable conflict/integrity incident，不重置旧事实。

`gate_integrity_ok` 是上述重放的合取，不相信 agent 文本或 projection 中的 PASS。

## 3. L1 policy conformance

兼容输出字段 `path_conformance_ok` 现在表示 policy conformance；`skipped_states` 保存具体 durable
policy failure，而不是宏观路径缺口。检查至少包括：

- outcome receipt 先于 controller handling event；所有 plan rejection 有稳定 reason codes；
- automatic retry 保留旧 `RETRY_WAIT`，只有一个 attempt+1 successor，冻结 manifest/policy，新 staging，
  executor attempt identity 不重复；
- probe resolution 与原 Indeterminate receipt、operation、idempotency、error/failure signature、retry policy
  一一绑定；`succeeded`/`absent`/`unknown` 分别进入 success/retry/block，probe 不作为普通成功；
- semantic repair 带 class/source/reason，旧 Action 不复活，run 保持 `RUNNING`，且 exactly one superseding
  plan/action/staging；integrity/unknown 保持 `BLOCKED`，只有 matching human unblock 才能 replacement；
- HITL 初始 Paused receipt 不改写，continuations 按 sequence 决定 effective outcome；STARTED 不确定性
  integrity block；effective success 仍经过 gate/intents/promotion；
- unblock request digest、source evidence、new plan/action/staging identity 匹配，semantic repair 不走 manual
  unblock；conflict history 不重置或复用；
- 非 probe 的 failed/paused/indeterminate outcome 不得标成 success；successful release 的全部 dependencies
  已 committed。

L1 verdict：run=`BLOCKED` 或任一 integrity/policy failure 为 FAIL；非 terminal 且无 failure 为 WARN；满足
completion predicate 且全部重放通过为 PASS。

## 4. L2 翻译质量

L2 保持既有行为：检查章节对应完整性、空译/漏译、长度与字符分布、术语 locked/preferred 约束、禁用
rendering、重复/截断迹象和可配置语言判定。模型 judge 只能补充语义评分，不能覆盖确定性失败。抽样与
阈值见 [`../QUALITY_SCORE.md`](../QUALITY_SCORE.md)。

## 5. L3 EPUB 与 release

L3 保持既有行为：EPUB 存在且能解析，publication lint/asset manifest/EPUBCheck evidence 已 committed；
随机抽检按确定性种子与 strata 选样，独立评审满足 avg/min/confidence/连续 PASS 阈值；release manifest、
版本、checksums 与所有 prerequisite Action committed。缺任一硬证据即 FAIL。

## 6. 数据集与回归

- L1 fixtures 必须通过 public RunLedger API 建立合法 baseline；负向 conformance 可使用 frozen typed
  model copies，不得写 raw SQLite。
- 每个测试命名它捕获的 production break，并先观察行为 RED。
- L2/L3 golden fixtures 与阈值变更需要单独决策记录；宏观编排重构不得静默改变其行为。
- `abi eval trace <project>` 输出 L1；`abi eval book <project>` 汇总三平面。

## 7. 源码入口

| 内容 | 文件 |
| --- | --- |
| typed durable eval facts | `src/abi/eval/run_facts.py` |
| L1 replay | `src/abi/eval/trace.py` |
| L2/L3 与汇总 | `src/abi/eval/book.py` |
| RunLedger typed reads | `src/abi/project/run_ledger.py` |
| quality thresholds | `docs/QUALITY_SCORE.md` |
