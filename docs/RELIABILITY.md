# RELIABILITY.md

> 翻译一本书可能要数小时、数千次 LLM 调用、数十美金。任何环节都会失败。
> 本文件定义**失败处理、检查点、幂等、重试、限流**。

## 1. 失败模式分类

| 类别 | 例子 | 策略 |
| --- | --- | --- |
| 瞬时网络 | 5xx, 超时 | 指数退避重试（最多 N 次） |
| 限流 | 429, RateLimit | token-bucket 等待 + 重试 |
| Schema 错 | LLM 返回非 JSON / 字段错 | RevisionTranslator 带反馈重试 |
| 内容错 | term_drift, length outlier | flag + RevisionTranslator |
| 拒答 | "I cannot translate this" | 改 prompt（去敏感词 / 上下文遮蔽）重试一次，仍拒 → flag |
| API key 失效 | 401, 403 | 立即停（无意义重试） |
| 余额不足 | 402 / 特定错误码 | 立即停 + 触发成本上限事件 |
| 输入解析 | pdf 损坏 | 失败前导出诊断 → 用户介入 |
| 内部 bug | KeyError 等 | 落 events，run 停，下次续跑跳过此段并 flag |

## 2. 重试策略

### 2.1 网络/限流

v0.1 用 LangChain `Runnable.with_retry(...)`：
- 退避：`wait_exponential_jitter=True`，base=2, max=30
- 最多次数：默认 3 次；429 单独走 token-bucket 等待，不计入重试计数
- 超时：单次 120 秒（`ChatOpenAI.request_timeout`）、整体 600 秒（`asyncio.wait_for`）
- 非可重试错误（4xx 中 401/403/404）立即失败，不重试

### 2.1.1 已知失败模式：LengthFinishReasonError

某些 OpenAI 兼容端点（如 DeepSeek-V4-Pro）在 json_mode 下输出 token 占用偏多。当 `max_output_tokens` 设得太小时，会触发 `openai.LengthFinishReasonError`。

v0.1 默认 `max_output_tokens=4096`。该错误目前走"agent 优雅降级"路径——例如 `chapter_summarizer` 失败时给该章一个空 abstract，整体流水线继续。

v0.2 计划：对 `LengthFinishReasonError` 单独识别，重试时自动把 `max_tokens` 提升 2 倍（最多 16k）。

### 2.2 内容错

不简单"再试一次"。重试时把上一次的输出 + 错误注入到 `RevisionTranslator` prompt：
```
上次的译文：<...>
检测到的问题：
- term_drift: "embodiment" 应译为「具身」而非「体现」
- length_ratio: 0.32 偏低
请基于这些反馈重新翻译。
```

最多 `max_revision_rounds = 2` 轮。

### 2.3 整段失败

某段达到所有重试上限仍失败：
- `TranslationUnit` 写入 `flags=[...]`，`translated_text` 设为 `source_text`（透传）
- 加 HTML 注释 `<!-- abi:flagged ... -->`
- 进入 `translate/flagged.jsonl`
- **不阻塞**其他段落

## 3. Checkpoint

### 3.1 颗粒度

- Pass 0：整体或不存在；不做段落级 checkpoint（重跑廉价）
- Pass 1：章节级 checkpoint（一章一文件）
- Pass 2：**段落级 checkpoint**（每段一文件，命名为 `<paragraph_id>.json`）
- Pass 3：无需 checkpoint（纯本地，秒级）

### 3.2 检测已完成

```python
def is_paragraph_done(run_dir, p: Paragraph) -> bool:
    f = run_dir / "translate" / "paragraphs" / f"{p.paragraph_id}.json"
    if not f.exists():
        return False
    unit = TranslationUnit.model_validate_json(f.read_text())
    # 还要检查 prompt_version / glossary_version 是否与当前一致
    return (unit.prompt_version == current_prompt_version()
            and unit.context_window.glossary_version >= current_glossary_version())
```

glossary 版本提升后，只重跑"用到了变化术语"的段落（按 `terms_used` 反向查询）。

### 3.3 续跑入口

`abi resume` 检测最近一次 run，自动判断该跑哪步：
- IR 缺失 → Pass 0 全跑
- Survey 缺失或不完整 → Pass 1 续跑
- 有未完成段落 → Pass 2 续跑
- 所有段落完成但 assemble 未跑 → Pass 3

## 4. 幂等

### 4.1 段落 ID 纯函数
见 `data-model.md §3`。保证同一段在同一 IR 下永远同 ID。

### 4.2 Run 目录隔离
每次新 run 用新 `run_id`，但**共享** `book-id` 下的同名段落文件可被跨 run 复用（通过 hard link 或 explicit copy）。
默认行为：续跑复用最近 run；用户可 `--isolate` 强制全新。

### 4.3 LLM 调用 cache（可选）

按 `(prompt_hash, model, temperature)` 缓存响应。
- 默认开启 in-memory cache（单次 run）
- `--cache-dir` 启用持久 cache（跨 run）
- temperature > 0.3 时自动禁用 cache（无意义）

## 5. 限流

### 5.1 客户端 token-bucket

`providers.llm` 内置：
- 全局 RPM / TPM 上限
- 按 provider / model 独立 bucket
- 配置：`providers.openai.rpm: 500`、`tpm: 600000`

### 5.2 自适应退让

收到 429 时：
- 该 bucket 立即减半 RPM 5 分钟
- 5 分钟后线性回升

### 5.3 并发 gate

`runtime` 维护全局 `asyncio.Semaphore(concurrency)`，章节并行受其限制。

## 6. 成本控制

### 6.1 预算 gate

```python
async def llm_call(...):
    expected_cost = estimate_cost(messages, model)
    if metrics.total_cost + expected_cost > config.cost.hard_cap_usd:
        raise BudgetExceeded(...)   # 触发优雅停
```

### 6.2 触发优雅停

`BudgetExceeded` 被 runtime 捕获：
- 取消正在跑的章节
- 等待已发起的调用完成（不放弃已花的钱）
- 写 checkpoint
- exit code 4

用户可以 `--max-cost-usd $N` 提升上限后续跑。

### 6.3 warn 阈值

到达 `warn_at_usd` 时打事件 + stderr 提示，但不停。

## 7. 可观测性

v0.1 采用**两层可观测**：

- **Langfuse**：所有 LLM 调用的 trace（prompt / completion / token / cost / latency / retry 链路），按 `run_id` 聚根，按 `book_id` / `paragraph_id` 检索
- **本地 `events.jsonl`**：业务级事件（段落、章节、glossary、checkpoint、预算）的真相源，离线可消费

详见 [`design-docs/tech-stack.md §4`](./design-docs/tech-stack.md#4-两套观测的分工)。

### 7.1 事件流

`runs/.../events.jsonl` 严格 JSON Lines，每行独立 parse。
事件类型至少包括：

| event | 含义 |
| --- | --- |
| `run.start` / `run.end` | 整体边界 |
| `pass.start` / `pass.end` | 每 pass 边界 |
| `agent.call` | LLM 调用（成功） |
| `agent.retry` | 重试发起 |
| `agent.failed` | 重试用尽 |
| `paragraph.translated` | Pass 2 段完 |
| `paragraph.flagged` | 段落被 flag |
| `glossary.updated` | term 入库 |
| `context.trimmed` | 上下文裁剪 |
| `budget.warn` / `budget.hard_cap` | 成本事件 |
| `checkpoint.saved` | checkpoint 落盘 |

### 7.2 metrics.json

实时聚合（每 30 秒刷一次）：
```json
{
  "started_at": "...",
  "updated_at": "...",
  "paragraphs": {"total": 3421, "done": 1208, "flagged": 4, "failed": 0},
  "tokens": {"input": 4123456, "output": 891234, "cached": 1234567},
  "cost_usd": 12.34,
  "duration_s": 1845
}
```

### 7.3 日志

`logs/info.log`、`logs/warn.log`、`logs/error.log`，结构化 JSON 行。

## 8. 安全失败

**默认偏保守**：
- 不删除已存在的 run 目录（手动 `abi runs rm` 才删）
- 不覆盖已存在的输出文件（除非 `--overwrite`）
- Ctrl-C 时优雅捕获 → checkpoint → 退出（exit 130）

## 9. 不变量

1. 任何运行结束（成功/失败/中断）必有完整 `events.jsonl` + `metrics.json`
2. 段落级文件落盘是**原子**的（先写 `.tmp`，再 `os.replace`）
3. checkpoint 在故障重启后保证"至少一致"语义：可能有重复翻译同一段（被覆盖），但不会遗漏
4. budget hard cap 永不超出（gate 在调用前）
