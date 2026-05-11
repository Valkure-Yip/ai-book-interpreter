# Design Docs Index

> 详细设计文档的索引页。**每个文档独立可读**，并显式声明前置阅读。

| 文档 | 一句话定位 | 前置 |
| --- | --- | --- |
| [`core-beliefs.md`](./core-beliefs.md) | 项目设计哲学，影响所有取舍 | 无 |
| [`tech-stack.md`](./tech-stack.md) | v0.1 技术选型（LangChain / OpenAI-compatible / Langfuse）与取舍 | core-beliefs |
| [`pipeline.md`](./pipeline.md) | 三遍流水线的完整定义（Pass 0/1/2/3） | core-beliefs |
| [`data-model.md`](./data-model.md) | Book IR、Glossary、TranslationUnit 等数据契约 | 无 |
| [`sliding-window.md`](./sliding-window.md) | Pass 2 的上下文构造算法 | pipeline, data-model |
| [`agent-architecture.md`](./agent-architecture.md) | 智能体循环、Provider 接口、prompt 模板 | pipeline, tech-stack |
| [`survey-design.md`](./survey-design.md) | Pass 1 的 map-reduce 摘要策略 | pipeline |
| [`assembly-design.md`](./assembly-design.md) | Pass 3 的多模式装配 | pipeline, data-model |
| [`ingest-design.md`](./ingest-design.md) | Pass 0：epub/pdf/txt 各自的解析策略 | data-model |

## 在哪里读什么

- 想理解**为什么这么设计** → `core-beliefs.md`
- 想理解**整个流程** → `pipeline.md`
- 想动手实现/修改 → 对应的具体 Pass 文档 + `data-model.md`
- 想加 LLM provider 或调 prompt → `agent-architecture.md`
