# Agent Architecture

> 智能体（agent）= 一个**有目标的 LLM 调用单元**。本系统中并非"一个大 agent 干所有事"，而是多个**职责单一**的小 agent，由 `runtime` 编排。
>
> **v0.1 实现说明**：每个 agent 在代码层是一个 LangChain LCEL chain（`prompt | llm.with_structured_output(Schema) | post`），通过 `providers/llm/get_chat_model()` 获取统一的 OpenAI 兼容客户端，Langfuse `CallbackHandler` 自动接管追踪。本文件的 `LLMClient` / `ChatResponse` 抽象是**语义契约**，v0.1 中具体由 LangChain 实现，未来可替换。详见 [`tech-stack.md`](./tech-stack.md)。

## 1. Agent 列表

| Agent | 输入 | 输出 | 用在哪 |
| --- | --- | --- | --- |
| `ChapterSummarizer` | 章节文本 | `ChapterSummary` | Pass 1 |
| `BookSynthesizer` | 所有 `ChapterSummary` | `BookOverview` | Pass 1 |
| `GlossaryArbiter` | term 候选冲突 | 最终 `GlossaryEntry` | Pass 1 + Pass 2 |
| `StyleGuideDeriver` | `BookOverview` + 用户偏好 | `StyleGuide` | Pass 1 |
| `ParagraphTranslator` | 滑动窗口 context | `TranslationUnit` | Pass 2（主力，调用最多） |
| `RevisionTranslator` | 失败的 `TranslationUnit` + 错误反馈 | 修正后的 `TranslationUnit` | Pass 2 重试 |
| `TermProposer` | 段落 + 未匹配的可疑术语 | `PendingTerm[]` | Pass 2 |
| `MindmapDrawer` | `BookOverview` | Mermaid 源码 | Pass 1 |
| `StructureClassifier`（可选） | pdf 文本块 | `kind` 标签 | Pass 0 |

每个 agent：
- 有一个**版本化的 prompt 模板**（`src/prompts/<agent>@vN.j2`）
- 有一个**严格的 pydantic 输出 schema**
- 通过 `providers.llm.chat(...)` 调用，不直接持有 SDK 实例

## 2. Provider 接口

### 2.1 `LLMClient`

```python
class LLMClient(Protocol):
    name: str
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        schema: type[BaseModel] | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
        cache_key: str | None = None,
    ) -> ChatResponse: ...

class ChatResponse(BaseModel):
    parsed: BaseModel | None       # schema 非 None 时
    text: str
    model: str
    token_usage: TokenUsage
    cost_usd: float
    latency_ms: int
    raw_provider_id: str           # 供应商侧 request id，便于排查
```

### 2.2 实现（v0.1）

v0.1 只有一种实现：**`OpenAICompatibleClient`**，本质是 `langchain_openai.ChatOpenAI` 的薄包装，由 `base_url` + `api_key` + `model` 驱动。
通过切换环境变量，同一份代码即可对接：

| 端点 | base_url |
|---|---|
| OpenAI 官方 | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| Together / Moonshot / SiliconFlow | 各自的 `/v1` |
| Ollama 本地 | `http://localhost:11434/v1` |
| vLLM / LiteLLM 网关 | `http://<host>:<port>/v1` |

测试用 `MockChatModel`（继承 LangChain `FakeListChatModel`），按 prompt hash 返回 fixture。

**实现要求**：
1. 内置 token-bucket 限流（`aiolimiter` + `asyncio.Semaphore`，按 model 独立）
2. 内置重试（用 `Runnable.with_retry(stop_after_attempt=N, wait_exponential_jitter=True)`；4xx 不重试）
3. Langfuse `CallbackHandler` 自动 attach（缺凭据时 no-op）
4. 业务事件落 `events.jsonl`（调用开始/结束/失败/重试）
5. 内置 token 计费：拿到响应后从 `response_metadata.token_usage` 累加 `metrics.json`；按 `providers/llm/pricing.py` 算 cost
6. 启动 self-check：探测当前 `(base_url, model)` 支持的结构化输出策略（`json_schema` → `tool_calling` → `json_mode` → `prompt_only`），缓存到 `manifest.json.capabilities`

### 2.3 Provider 配置（v0.1）

```yaml
# config 中
llm:
  base_url: https://api.openai.com/v1   # 由 LLM_BASE_URL env 覆盖
  api_key_env: LLM_API_KEY
  model: gpt-4o-mini
  temperature: 0.2
  request_timeout_s: 60

observability:
  langfuse:
    enabled: true
    host: https://cloud.langfuse.com
    public_key_env: LANGFUSE_PUBLIC_KEY
    secret_key_env: LANGFUSE_SECRET_KEY
    upload_full_payload: false           # 默认仅 metadata，不上传 prompt/completion
```

`providers.llm.get_chat_model(config)` 是唯一入口。其他层**不得**直接 import `langchain_openai` 或 `langchain` 任何子模块。
由 linter `tools/lint/no_direct_sdk.py` 与 `tools/lint/no_direct_chat_model.py` 强制。

## 3. Prompt 工程规范

### 3.1 模板文件位置

```
src/prompts/
├── chapter-summarizer/
│   ├── v1.j2
│   ├── v2.j2
│   └── current -> v2.j2     # 符号链接，CI 校验
├── paragraph-translator/
│   ├── v1.j2
│   └── ...
└── README.md
```

### 3.2 模板要求

- Jinja2，渲染时所有插入值必须 escape
- 顶部注释说明：版本号、生效时间、与上一版的差异
- 单元测试：每个版本至少 3 个 fixture，校验渲染稳定性

### 3.3 Prompt 结构标准

每个 agent prompt 都遵循同一骨架：

```
<system>
你是 <role>。<task one-liner>.
约束：
- 只输出符合 schema 的 JSON
- 不解释、不寒暄
- 遇到歧义按 <fallback policy>
</system>

<user>
## Inputs
<结构化输入区，明确字段>

## Task
<具体要做什么>

## Output Schema
<pydantic schema 的人类可读描述>
</user>
```

**反模式**（禁止）：
- "请帮我..."、"如果可以的话..."（弱化指令）
- 把多个不同任务塞在同一 prompt
- 在 prompt 中混杂中英以外的第三语言（除非翻译 target 是该语言）

### 3.4 版本切换

切版本需要：
1. 新版本文件 + 旧版本保留
2. `current` 符号链接更新
3. 在 `docs/exec-plans/active/` 加一份"切版本计划"，含 A/B 对比的样本结果
4. 旧版本至少保留 3 个月（用于回放历史 run）

## 4. Agent Loop 模式

### 4.1 一次成功路径

```
agent(input) → llm.chat(schema=Schema) → response.parsed → validate(parsed)
  → if ok: return
```

### 4.2 重试路径（带反馈）

```
agent(input) → llm.chat() → parsed
  → validate(parsed) returns errors
  → if retries < max:
       agent(input, error_feedback=errors) → llm.chat()
       (用 RevisionTranslator 的 prompt 模板，注入具体错误信息)
  → return last (with flags 非空)
```

**关键**：重试不是简单"换个温度再试"——而是**把上一次的错误作为输入**，让 LLM 知道要修正什么。
这是受 Codex 文章 "Ralph Wiggum 循环" 启发：让 agent 看到自己的 review 反馈，再自我修正。

### 4.3 自我审查（可选，高质量模式）

`--quality=high` 时启用：
- `ParagraphTranslator` 产出后，调 `TranslationReviewer`（用更便宜的模型）打分 + 提改进点
- 分数低于阈值 → 再调一次 `RevisionTranslator`
- 终止条件：分数达标 或 已达 max 轮次

## 5. 与"代码仓库即记录系统"的一致性

每个 agent 调用都在 `events.jsonl` 留下：
```json
{"event": "agent.call", "agent": "ParagraphTranslator",
 "prompt_version": "v3", "paragraph_id": "...",
 "tokens": {...}, "cost_usd": ..., "outcome": "ok"|"flagged"|"failed"}
```

**任何对 prompt 的修改、对模型的切换、对 schema 的变更，都会反映在这条事件流里**——让后续任何人/智能体都能回溯"为什么这本书在 6 月 12 日的运行里术语漂移变多了"。

## 6. 不变量

1. 任何 LLM 调用必须经过 `providers.llm.get_client()`；linter 强制。
2. 任何 LLM 输出必须经过 pydantic schema 验证；裸 dict 禁止跨层传递。
3. 每个 agent 必须有版本号；版本号写入 `RunManifest.prompt_versions`。
4. 重试必须带错误反馈（不允许盲目重试）。
5. 失败的 agent 调用必须 `events.jsonl` 落事件 + 在工件中可见（flags 或 errors.jsonl）。
