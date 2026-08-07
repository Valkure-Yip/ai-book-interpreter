# Core Beliefs

> 这些信念是 ABI 做设计取舍时的前提。具体执行协议以
> [`dynamic-agent-orchestration.md`](./dynamic-agent-orchestration.md) 为准。

## 1. 翻译质量来自全书证据，而非单次模型规模

模型需要作者语气、体裁、本书研究、试译经验、术语和相邻语境，但不应在每次翻译调用中接收整套
QA、EPUB 与发布规则。ABI 把长期上下文沉淀为 `metadata/`、`glossary/` 和 `qa/` 下的版本化工件；
每章翻译只注入原文、5–8 条关键文体规则与命中术语。

## 2. 术语和文体必须成为显式工件

同一术语跨章漂移会破坏整本书。全局研究、本书研究、style profile、style guide 与 terms.csv 都是
后续 Action 的 committed prerequisites，而不是隐藏在对话历史中的记忆。机械 validator 与独立评审共同
检查一致性。

## 3. 章节是调度与修复单元，段落是稳定证据单元

章节具备可独立授权的 read/write set，适合翻译、章控、评审和并行调度；段落 ID 仍是原文内容与位置的
稳定函数，用于采样、定位缺陷和质量证据。不同章节可并行，同章或目录/后代路径冲突必须互斥。

## 4. 工件是 agent 之间的接口

任何“模型应当知道”的信息都必须显式进入 prompt、skill 或可读工件。agent 的消息历史和 LangGraph
checkpoint 只帮助恢复运行时游标，不能充当业务完成事实。跨 Action 的知识通过强类型、可校验的文件与
RunLedger provenance 传递。

## 5. 仓库就是记录系统

设计、产品行为、运行协议、质量标准和执行记录都以可被人和 agent 直接消费的形式入库。口头约定不能
替代 schema、文档、测试或可执行门禁。

## 6. 渐进式披露优于大而全

`AGENTS.md` 只负责导航；架构、可靠性、质量、安全和产品行为分别进入专门文档。Action harness 同样按
能力渐进加载 skills 与工具，避免把全项目规则塞进每次调用。

## 7. 不变量优先于固定流程

ABI 不规定唯一的 happy path。Planner 可以动态选择、重排或重复 eligible Action，但依赖、权限、预算、
expected manifest、validator 和 completion predicate 都由确定性代码强制。模型不能直接改变 run、Action、
gate 或成功状态。

## 8. 成功必须可证明，未知必须阻断

“agent 说完成了”不是成功。每个普通成功都必须有 immutable outcome receipt、exact staged bundle、PASS
gate、完整 promotion intents、canonical postcheck 与最终 ledger transaction。崩溃恢复无法证明结果时
fail closed；不得用旧 canonical 文件或推测补写成功。

## 9. 重试、修复和人工恢复是三种不同协议

瞬时失败只有 frozen retry policy 允许时才能创建同 Action 的下一 attempt；语义缺陷创建新 plan/action/
staging；integrity 或未知分类进入 `BLOCKED`，只能带明确证据人工恢复。三者都保留旧 receipt 与历史，
不覆盖失败事实。

## 10. 双语和多审证据必须可追溯

原文章节始终保存在 `chapters/src/`，译文依次进入 `translated/`、`controlled/`、`final/`。忠实度、
可读性/意象、术语、独立 agent A/B 与随机抽检报告都绑定具体 committed 工件，便于定位和复核。

## 11. 成本、可观测性与质量同等重要

所有 LLM 调用都必须经过 providers，受 BudgetGate 约束并写入 Langfuse 与本地事件。Langfuse 可以安全
降级，但本地 durable facts 不能丢；正文是否上传由显式 payload 开关控制。

## 12. 发布是门禁结果，不是模型决定

只有最终章节、制作规格、EPUB 构建、publication lint、asset manifest、EPUBCheck、独立评审与随机抽检
满足 completion predicate，run 才能 `COMPLETED`。release 只是把已通过门禁的 EPUB 复制到版本化位置并
记录状态，不能绕过质量链。
