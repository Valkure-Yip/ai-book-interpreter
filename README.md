# AI Book Interpreter

> 一个面向**学术书籍**的自动翻译 AI 智能体：输入 `txt` / `epub` / `pdf`，输出高质量 Markdown 译本，并可附带书籍要点总结与思维导图。

本项目以 [OpenAI 工程技术：在智能体优先的世界中利用 Codex](https://openai.com/zh-Hans-CN/index/harness-engineering/) 为工程范式：**人类掌舵，智能体执行**。
代码仓库本身即"记录系统"——所有设计、规范、规则都被组织为智能体可读、可机械化执行的工件。

---

## 一句话定位

> 不是把 LLM 调用包一层 CLI，而是构建一个**可恢复、可观测、可强制不变量**的翻译流水线，让翻译质量随着上下文积累而**单调收敛**。

## 核心特性

| 特性 | 说明 |
| --- | --- |
| **三遍流水线** | 通读（survey）→ 段落翻译（translate）→ 装配输出（assemble） |
| **滑动窗口上下文** | 每段翻译都注入前 K 段译文 + 后 J 段原文 + 章节摘要 + 全局术语表 |
| **术语锁定** | Pass 1 产出全局术语表，Pass 2 强制遵循；自定义 linter 机械校验 |
| **多种输出形态** | 仅译本 / 双语对照 / 带 AI 摘要与思维导图的增强版 |
| **可恢复** | 段落级 checkpoint，崩溃后续跑；段落 ID 由内容哈希决定，幂等 |
| **可观测** | 业务事件 → 本地 `events.jsonl`；LLM 调用 → Langfuse trace（按 `book_id` / `paragraph_id` 检索） |
| **OpenAI 兼容统一接入** | 一份代码对接 OpenAI / DeepSeek / Together / Moonshot / Ollama / vLLM 等，靠 `base_url` 切换 |
| **LangChain 编排** | LCEL chains + 结构化输出 + 自动重试，业务层只见我们自己的语义抽象 |

## 文档地图

> 本文件只做入口。要深入任何主题，请按 [`AGENTS.md`](./AGENTS.md) 的索引继续阅读。

- [`AGENTS.md`](./AGENTS.md) — 智能体/贡献者的"地图"，约 100 行
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — 系统架构总览与分层依赖图
- [`docs/`](./docs) — 全部设计文档、产品规范、执行计划与质量/可靠性/安全规范

## 快速开始（v0.1 目标）

```bash
# 配置 LLM 端点（任何 OpenAI 兼容均可）
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_API_KEY=sk-...

# 可选：开启 Langfuse 追踪（缺凭据时自动跳过，不报错）
export LANGFUSE_PUBLIC_KEY=pk-...
export LANGFUSE_SECRET_KEY=sk-...

# 仅译本（EPUB）
abi translate book.epub -o book.zh.md --target zh --model gpt-4o-mini

# 双语对照（TXT）
abi translate book.txt -o ./out --mode bilingual --target zh --model gpt-4o-mini

# 用 DeepSeek 兼容端点
abi translate book.epub --base-url https://api.deepseek.com/v1 --model deepseek-chat

# 仅做 Pass 1（不翻译，只产出摘要与术语表）
abi survey book.epub -o ./out
```

v0.1 支持的输入：**EPUB / TXT**（PDF 路线见 v0.2）。

详见 [`docs/product-specs/cli-and-config.md`](./docs/product-specs/cli-and-config.md)。

## 工程理念（摘要）

1. **代码仓库即记录系统**：知识不存于 Slack 或脑中，而以 Markdown / 模式 / 可执行计划形式入库。
2. **渐进式披露**：`AGENTS.md` 是地图，不是百科全书。智能体按需深入。
3. **规范架构**：固定分层 + 单向依赖，由自定义 linter 机械执行。
4. **品味即不变式**：把"该这么做"编码成机器可校验的规则，而不是约定。
5. **吞吐量改变合并理念**：短生命周期 PR、非阻塞 CI、随时清理"AI 残渣"。

完整理念见 [`docs/design-docs/core-beliefs.md`](./docs/design-docs/core-beliefs.md)。

## 当前状态

- **版本**：v0.1 设计阶段（仅文档，尚无代码）
- **下一步**：执行 [`docs/exec-plans/active/v0.1-mvp.md`](./docs/exec-plans/active/v0.1-mvp.md)

## License

TBD
