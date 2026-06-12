# Tech Stack（v0.1 锁定）

> 本文件记录 v0.1 的技术选型与**取舍理由**。所有跨层影响（依赖、lint 白名单、provider 接口）以本文件为准。
> 变更选型 = 破坏性变更，需新计划 + 迁移说明。

## 概览

| 关注点 | v0.1 选型 | 备选（未来） |
|---|---|---|
| LLM 编排 | **LangChain (LCEL)** | LlamaIndex / DSPy / 裸 SDK |
| LLM 协议 | **OpenAI-compatible Chat Completions** | Anthropic Messages、Gemini、自研协议 |
| 观测 / 追踪 | **Langfuse** | OpenTelemetry + Phoenix / LangSmith / 自建 |
| Prompt 管理 | 仓库内 Jinja2 + Langfuse Prompt Mgmt（可选） | 纯 Langfuse / 纯本地 |
| 数据模型 | pydantic v2 | dataclasses、attrs |
| 输入解析 | ebooklib + bs4（EPUB）、内置启发式（TXT） | unstructured.io |
| 输出装配 | jinja2 + markdown-it-py | pandoc |
| 测试 | pytest + pytest-asyncio + respx | unittest |

## 1. 为什么选 LangChain

### 我们要的能力
1. **prompt 模板**：版本化、可测试、与代码解耦
2. **结构化输出**：强制 pydantic schema，失败时可重试
3. **链路组合**：prompt → llm → parser → validator → retry，可读、可测
4. **回调/钩子**：让 Langfuse 自动接管追踪
5. **并发原语**：与 `asyncio` 协作良好

### LangChain 的现状评估
- **LCEL**（`prompt | llm | parser` 风格）是稳定的、文档完备的核心；legacy chains 已被弃用
- `langchain-openai` 内置 `with_structured_output()`，自动选择 `json_schema` / `function_calling` / `json_mode` 三种策略
- `Runnable.with_retry()` / `with_fallbacks()` 是组合原语，与"业务级重试（带反馈）"互补
- Langfuse 通过 `CallbackHandler` 一行接入

### 反对意见与回应
- "LangChain 抽象过重，黑魔法多" → 我们**只用 LCEL + structured output + retry**，不碰 agents / tools / memory 等高层抽象
- "锁定到 LangChain 风险" → 抽象在 `providers/llm/` 内部；业务层只见我们的 `invoke_structured(chain, input, schema)`，可在未来替换
- "性能开销" → 与 LLM 调用 latency（秒级）相比可忽略

### 不变量（lint 强制）
- `langchain*` 包**仅可**在 `src/providers/**` 中 import（D2 白名单扩展）
- 业务层（survey / translate / assemble）**不得**直接构造 `ChatOpenAI` 或 `PromptTemplate`，必须经 `providers/llm` 工厂
- 新 lint：`tools/lint/no_direct_chat_model.py`

## 2. 为什么用 OpenAI-compatible 统一接入

### 现状
Chat Completions API 已成事实标准。下列均提供官方或社区维护的兼容端点：

| 来源 | base_url 形态 |
|---|---|
| OpenAI 官方 | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| Together.ai | `https://api.together.xyz/v1` |
| Moonshot Kimi | `https://api.moonshot.cn/v1` |
| SiliconFlow | `https://api.siliconflow.cn/v1` |
| Ollama 本地 | `http://localhost:11434/v1` |
| vLLM 自建 | `http://<host>:<port>/v1` |
| LiteLLM 网关 | `http://<host>:4000/v1`（可代理任意非兼容模型） |

### 收益
- **一份代码，N 个 provider**：切换仅改 `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`
- **离线可跑**：本地 Ollama / vLLM 完全离线，敏感书籍可用
- **未来扩展平滑**：Anthropic / Gemini 通过 LiteLLM 代理转换为兼容形式

### 已知不兼容差异（要在 v0.1 处理）

不同端点对"结构化输出"的支持差异最大：

| 策略 | OpenAI | DeepSeek | Ollama | 其他兼容 |
|---|---|---|---|---|
| `response_format=json_schema` | ✅ | 部分支持 | ❌ | 不一定 |
| Function calling / tool use | ✅ | ✅ | 部分 | 多数 |
| JSON mode（自由 JSON） | ✅ | ✅ | ✅ | 多数 |
| 纯 prompt + 后处理 | ✅ | ✅ | ✅ | ✅ |

`providers/llm/structured.py` 实现**自动降级**：
```
json_schema → tool_calling → json_mode → prompt-only
```
启动时对配置的 `(base_url, model)` 探测一次，缓存能力描述符到 `runs/<run-id>/manifest.json`。

### 配置形式

```yaml
llm:
  base_url: https://api.openai.com/v1  # 由 LLM_BASE_URL env 覆盖
  api_key_env: LLM_API_KEY
  model: gpt-4o-mini
  temperature: 0.2
  max_output_tokens: 2048
  request_timeout_s: 60
  # 探测得到的能力（启动时自动填）：
  capabilities:
    structured_output: json_schema  # | tool_calling | json_mode | prompt_only
    prompt_caching: false
```

不使用历史的多 provider 区分（`provider: openai` vs `provider: deepseek`）——v0.1 只有一个"OpenAI 兼容"provider，靠 base_url 区分。

## 3. 为什么选 Langfuse

### 我们要的能力
1. 每次 LLM 调用的完整 trace（inputs / outputs / model / tokens / latency / cost）
2. 按业务实体（book / chapter / paragraph）聚合检索
3. Prompt 版本管理（可选）
4. Dataset + 评测回归（v0.2 用）
5. 开源、可自托管，不锁定云端

### 与 LangChain 的集成

```python
from langfuse.callback import CallbackHandler

handler = CallbackHandler(
    public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
    secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
)

# 在 chain.invoke 时：
result = chain.invoke(
    input_data,
    config={
        "callbacks": [handler],
        "metadata": {
            "book_id": book_id,
            "run_id": run_id,
            "paragraph_id": paragraph_id,
            "pass": "translate",
            "prompt_version": "paragraph-translator@v1",
        },
        "tags": ["pass-2", "translate"],
    },
)
```

### Trace 结构

```
trace (root)         id = run_id
├── span "pass-1"
│   ├── span "chapter-summarizer:ch-01"
│   │   └── generation (LLM call)
│   └── ...
├── span "pass-2"
│   └── span "paragraph:ab12...-00047"
│       ├── generation (initial translate)
│       └── generation (revision, if any)
└── span "pass-3"
```

### 数据安全权衡（关键决策）

由 `observability.langfuse.upload_full_payload` / `LANGFUSE_FULL_PAYLOAD` 单一开关切换：

| 取值 | 行为 | 适用场景 |
|---|---|---|
| `true` / `1` | 上传完整 prompt、completion、messages | 开发期 prompt 调试；个人书库；自托管 Langfuse |
| `false` / `0` | 用 Langfuse `mask` 钩子把字符串内容替换为 `[REDACTED]`，保留消息结构与 token / latency / model 等 metadata | 处理版权书籍；合规环境 |

实现：
- `providers/observability/langfuse_client.py::build_langfuse_handler` 根据开关，按需把 `_redacting_mask` 作为 `mask=` 注入 `CallbackHandler`。
- mask 函数递归遍历输入：`str → [REDACTED]`、`list/dict → 递归`、`role`/`type` 等结构 key 保留、原始数字/布尔保留。
- 段落 ID / 章节 ID / book_id 是 hash，本身无内容信息，由本地 `events.jsonl` 承担——不依赖 Langfuse 是否上传全文。
- CLI 启动横幅 + run 结尾摘要均显式打印 `payload=full|redacted`，避免"以为开了其实没开"。
- run 结束 `router.flush()` 阻塞 Langfuse client，避免短任务 trace 丢失。

### 降级行为

- 缺 Langfuse 凭据 → handler 为 no-op，本地 `events.jsonl` / `metrics.json` 仍正常工作；CLI 显示 `disabled (missing env: ...)`。
- 凭据无效（`auth_check` 失败）→ 同上，`disabled (auth_check failed)`。
- Langfuse 不可达 → 异步上传失败静默重试；本地事件流为真相源。

## 4. 两套观测的分工

| 关注点 | Langfuse | 本地 `events.jsonl` |
|---|---|---|
| LLM 调用层（prompt/response/token/cost） | ✅ 主 | 摘要 |
| 业务事件（paragraph.translated、glossary.updated、checkpoint.saved） | ❌ | ✅ 主 |
| 跨 run 检索 | ✅ | grep |
| 离线分析 | 导出 | ✅ |
| CI / 自动化消费 | API 不便 | ✅ 主 |
| 人工排查体验 | ✅ UI | text |

两者**不重复**，互为补充。

## 5. 关键依赖版本（pin）

```toml
# pyproject.toml 摘录
[project]
requires-python = ">=3.11"
dependencies = [
  "pydantic>=2.7,<3",
  "langchain>=0.3,<0.4",
  "langchain-openai>=0.2,<0.3",
  "langfuse>=2.50,<3",
  "typer>=0.12,<1",
  "rich>=13,<14",
  "ebooklib>=0.18,<0.19",
  "beautifulsoup4>=4.12,<5",
  "markdown-it-py>=3,<4",
  "jinja2>=3.1,<4",
  "chardet>=5,<6",
  "httpx>=0.27,<0.28",
  "aiolimiter>=1.1,<2",
]
[dependency-groups]
dev = [
  "pytest>=8",
  "pytest-asyncio>=0.23",
  "respx>=0.21",
  "ruff>=0.6",
  "mypy>=1.11",
]
```

LangChain 在 0.3 之后 LCEL API 稳定；pin 到 0.3.x 兼容范围。

## 6. 变更管理

修改本文件 = 修改技术契约：
1. 必须先在 `docs/exec-plans/active/` 新增一份"切换计划"
2. 必须更新所有受影响的 lint 白名单
3. 必须为旧实现保留 ≥ 1 个 minor 版本的兼容路径
4. 必须在 CHANGELOG 顶部用 "BREAKING" 标记

## 7. 与已有设计文档的关系

| 已有文档 | 本文件如何影响它 |
|---|---|
| `agentic-pipeline.md` §1 providers | `LLMClient` = `langchain.BaseChatModel`；`ChatResponse` = LangChain 的 `AIMessage` + 解析后的 pydantic 实例 |
| `RELIABILITY.md` §2 重试 | tenacity 替换为 `Runnable.with_retry`；业务语义重试（带错误反馈）走阶段 runner 的校验回灌 |
| `RELIABILITY.md` §7 可观测性 | events.jsonl 仍主导业务事件；LLM 链路改由 Langfuse 提供 |
| `agentic-pipeline.md` §3 Prompt 设计 | 本地 Jinja2 仍是真相源；Langfuse Prompt Mgmt 是可选镜像（同步推送） |
| `SECURITY.md` §1 API key | 新增"Langfuse keys 也走 env，绝不入仓" |
| `product-specs/cli-and-config.md` | provider 列表收敛为"OpenAI-compatible"；增加 `LLM_BASE_URL` 环境变量与 `--base-url` flag |
