# ARCHITECTURE.md

> 本文件描述 **AI Book Interpreter** 的整体架构、分层与依赖规则。
> 任何代码变更不得违反本文件中定义的不变量，否则会被自定义 linter 拒绝。
>
> 详细的设计动机见 [`docs/design-docs/`](./docs/design-docs/)；
> 详细的强制规则见 [`docs/DESIGN.md`](./docs/DESIGN.md)。

## 1. 高层视角

系统是一个**有状态的批处理流水线**，对单本书运行三个顺序 pass，每个 pass 产出可被下一个 pass 与最终装配步骤消费的结构化工件。

```
┌──────────┐    ┌──────────────┐    ┌─────────────────┐    ┌──────────────┐
│  原书    │───▶│  Pass 0      │───▶│  Pass 1         │───▶│  Pass 2      │
│ txt/epub │    │  Ingest      │    │  Survey         │    │  Translate   │
│  pdf     │    │  → Book IR   │    │  → glossary,    │    │  → 段译文     │
└──────────┘    └──────────────┘    │    summaries,   │    │   (滑动窗口)  │
                                    │    mindmap,     │    └──────┬───────┘
                                    │    style guide  │           │
                                    └─────────────────┘           ▼
                                                          ┌──────────────┐
                                                          │  Pass 3      │
                                                          │  Assemble    │
                                                          │  → md outputs│
                                                          └──────────────┘
```

所有 pass 的输入输出都是**仓库内的版本化工件**（`runs/<book-id>/<run-id>/`），任一步骤崩溃后可以无副作用地从 checkpoint 续跑。

## 2. 分层架构

> 仿照 OpenAI Codex 的"严格边界与可预测结构"原则：业务领域内**只能向前依赖**一组固定层；横切关注点通过单一显式接口 `providers` 进入。

```
┌─────────────────────────────────────────────────────────────────┐
│ cli            (typer entry points, argument parsing)           │
├─────────────────────────────────────────────────────────────────┤
│ runtime        (job orchestration, checkpoint, retry, queue)    │
├─────────────────────────────────────────────────────────────────┤
│ assemble       (Pass 3: markdown composer, bilingual layouter)  │
├─────────────────────────────────────────────────────────────────┤
│ translate      (Pass 2: sliding-window paragraph translator)    │
├─────────────────────────────────────────────────────────────────┤
│ survey         (Pass 1: hierarchical summarizer, glossary)      │
├─────────────────────────────────────────────────────────────────┤
│ ir             (Pass 0: parsers → Book IR; epub/pdf/txt)        │
├─────────────────────────────────────────────────────────────────┤
│ config         (typed configuration, defaults, presets)         │
├─────────────────────────────────────────────────────────────────┤
│ types          (pure pydantic models, no I/O, no side-effects)  │
└─────────────────────────────────────────────────────────────────┘

                 ┌─────────────────────────┐
                 │ providers (横切)         │
                 │ - llm (OpenAI/Anthropic │
                 │        /DeepSeek/Ollama)│
                 │ - storage (fs/s3)       │
                 │ - telemetry (events,    │
                 │   metrics, cost)        │
                 └─────────────────────────┘
                            ▲
                            │ 任何上层只能通过此接口
                            │ 访问外部世界
```

### 2.1 单向依赖规则

- 上层可以 import 下层；下层**不得**import 上层。
- 任何模块（除了 `cli`）**不得**直接 import 外部 SDK（如 `openai`、`anthropic`）——必须经过 `providers`。
- 任何 LLM、文件、网络、时间相关副作用必须通过 `providers` 注入；`types/` 与 `config/` 必须保持纯函数/纯数据。

由 `tools/lint/layered_imports.py` 在 CI 中机械执行。

### 2.2 每层的职责与不职责

| 层 | 职责 | 不得做 |
| --- | --- | --- |
| `types` | pydantic 模型、枚举、领域类型 | 任何 I/O |
| `config` | 配置解析、默认值、preset | 业务逻辑 |
| `ir` | 解析 epub/pdf/txt → `Book` IR | 翻译、调用 LLM |
| `survey` | Pass 1：摘要、术语表、思维导图 | 翻译段落 |
| `translate` | Pass 2：段落级翻译 + 滑动窗口 | 解析输入文件、装配最终输出 |
| `assemble` | Pass 3：markdown / bilingual / annotated 装配 | 调用 LLM 做翻译 |
| `runtime` | job 编排、checkpoint、限流、并发 | 直接拼 prompt |
| `cli` | typer 命令、参数 → runtime | 业务逻辑 |
| `providers` | LLM client、存储、遥测的具体实现 | 业务决策 |

## 3. 工件与目录布局（运行时）

```
runs/
└── <book-id>/                   # book-id = sha1(原书内容)[:12]
    └── <run-id>/                # run-id = 时间戳 + 随机后缀
        ├── manifest.json        # 本次运行的配置、版本、provider
        ├── ir/
        │   └── book.json        # Pass 0 产出：完整 Book IR
        ├── survey/
        │   ├── overview.md
        │   ├── chapters/<n>.md
        │   ├── glossary.json    # 全局术语锁
        │   ├── style-guide.md
        │   └── mindmap.mmd
        ├── translate/
        │   ├── paragraphs/<id>.json   # 每段独立文件，便于增量重跑
        │   └── pending-terms.jsonl    # 翻译时新发现的术语候选
        ├── assemble/
        │   ├── translated.md
        │   ├── bilingual.md
        │   └── annotated.md
        ├── checkpoints/
        │   └── state.json       # 续跑用
        ├── events.jsonl         # 结构化事件流
        ├── metrics.json         # token、cost、耗时聚合
        └── logs/<level>.log
```

**设计原则**：每个工件都是**人类与智能体都可读**的；不存在不可解释的二进制状态。

## 4. 关键数据契约

详见 [`docs/design-docs/data-model.md`](./docs/design-docs/data-model.md)。摘要：

- `Book` = `BookMeta + Section[]`
- `Section` = 层级化，含 `heading_trail`、`Paragraph[]`、`Section[]`（子节）
- `Paragraph` = `{id, kind, source_text, anchors, position}`，`kind ∈ {prose, code, quote, list, figure_caption, footnote, equation, table_cell}`
- `Glossary` = `{term, source, target, definition, locked: bool, first_seen: paragraph_id}[]`
- `TranslationUnit` = 一个段落的翻译输出，包含 `translated_text`、`terms_used`、`confidence`、`notes`、`token_usage`

**段落 ID 是纯函数**：`paragraph_id = sha1(canonicalize(text))[:10] + "-" + zero_padded_position`。
这保证：相同输入 → 相同 ID → 可幂等重跑、可缓存。

## 5. 并发模型

- Pass 0：单线程（解析重 I/O 但顺序）。
- Pass 1：按章节并行 map，最终 reduce 成全书摘要；并发度由 `runtime.concurrency.survey` 控制。
- Pass 2：**章节内串行、章节间可并行**；章节内串行是为了滑动窗口能引用"刚翻好的"前文。可通过配置开启"乐观并行"模式（前文窗口只用源文）。
- Pass 3：单线程装配。

限流由 `providers.llm` 内部的 token-bucket 实现，全局共享。

## 6. 可观测性

每个 LLM 调用、每次重试、每个段落翻译都写入 `events.jsonl`，格式：

```json
{"ts": "...", "run_id": "...", "event": "paragraph.translated",
 "paragraph_id": "ab12...-00045", "model": "gpt-5",
 "tokens": {"in": 1820, "out": 410}, "cost_usd": 0.0123,
 "latency_ms": 4210, "retries": 0, "confidence": 0.92}
```

智能体（包括开发者本人）可以用 `jq` / `grep` / 简单脚本直接消费这些事件来诊断质量退化。**这是"为智能体可读性优化"的体现**。

## 7. 何时违反这些规则

短期实验、spike、概念验证可以放在 `experiments/` 顶级目录中，**不受**本文件约束。但任何要进入 `src/` 的代码必须遵守。

## 8. 与 OpenAI Codex 工程规范的映射

| Codex 规范 | 本项目落地 |
| --- | --- |
| 仓库即记录系统 | `docs/` + `runs/` 都是版本化、可被智能体直接消费的工件 |
| 渐进式披露 / 短 AGENTS.md | `AGENTS.md` ~100 行，仅作目录 |
| 严格分层 + linter 强制 | 第 2 节 + `tools/lint/layered_imports.py` |
| 边界处解析数据 | 所有 LLM 输出、文件输入、CLI 参数均用 pydantic 解析 |
| 智能体可读性优先 | `events.jsonl`、`runs/`、`docs/generated/db-schema.md` 等 |
| 品味即不变式 | 段落 ID 算法、glossary lint、长度比检查均为机械规则 |
| 垃圾回收 | `tools/garden/` 中的定期任务清理过时摘要、孤立段落、未引用术语 |
