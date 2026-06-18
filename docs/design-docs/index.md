# Design Docs Index

> 详细设计文档的索引页。**每个文档独立可读**，并显式声明前置阅读。

| 文档 | 一句话定位 | 前置 |
| --- | --- | --- |
| [`agentic-pipeline.md`](./agentic-pipeline.md) | **权威总览**：架构图 + 28 态流程图 + prompt/翻译方法论 | core-beliefs, tech-stack |
| [`langgraph-and-state-machine.md`](./langgraph-and-state-machine.md) | LangGraph ReAct 单元如何组建 28 态流水线 + 三层持久化（checkpointer/store 取舍） | agentic-pipeline, tech-stack |
| [`core-beliefs.md`](./core-beliefs.md) | 项目设计哲学，影响所有取舍 | 无 |
| [`tech-stack.md`](./tech-stack.md) | 技术选型（LangChain/LangGraph / OpenAI-compatible / Langfuse）与取舍 | core-beliefs |
| [`data-model.md`](./data-model.md) | Book IR 等数据契约 | 无 |
| [`ingest-design.md`](./ingest-design.md) | epub/pdf/txt 各自的解析策略 | data-model |

> v0.1 的三遍流水线文档（pipeline / sliding-window / agent-architecture /
> survey-design / assembly-design）已随 v0.2 agentic 重构删除，整体设计以
> [`agentic-pipeline.md`](./agentic-pipeline.md) 为准。

## 在哪里读什么

- 想理解**为什么这么设计** → `core-beliefs.md`
- 想理解**当前整个架构与流程** → `agentic-pipeline.md`
- 想动手实现/修改 → `agentic-pipeline.md` 末尾的「相关源码入口」+ `data-model.md`
- 想加 LLM provider 或调技术栈 → `tech-stack.md`
