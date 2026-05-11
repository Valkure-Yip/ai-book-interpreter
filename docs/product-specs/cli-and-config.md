# CLI and Configuration

## 命令总览

```
abi <subcommand> [options]
```

| 子命令 | 用途 |
| --- | --- |
| `abi translate` | 跑完整三遍流水线 |
| `abi survey` | 仅跑 Pass 0+1（产出概要、思维导图、术语表） |
| `abi resume` | 从最近的 run 续跑 |
| `abi runs ls` | 列出本地 run |
| `abi runs show <run-id>` | 查看某 run 的 manifest + 报告 |
| `abi glossary edit <book-id>` | 启动编辑器编辑术语表，保存后自动重跑受影响段落 |
| `abi config` | 查看/设置配置 |
| `abi cost` | 汇总历史成本 |

## `abi translate`

```
abi translate <input> [-o OUTPUT] [options]
```

### 主要参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `<input>` | — | 输入文件（txt/epub/pdf） |
| `-o, --output` | `./out` | 输出目录或单文件路径 |
| `--target` | `zh` | 目标语言（ISO 639-1） |
| `--source-lang` | auto | 源语言（自动检测） |
| `--mode` | `translated` | 一个或多个：`translated,bilingual,annotated,survey-only` |
| `--base-url` | `$LLM_BASE_URL` | OpenAI 兼容端点（如 `https://api.deepseek.com/v1`） |
| `--model` | 配置默认 | 模型名（如 `gpt-4o-mini`、`deepseek-chat`） |
| `--quality` | `standard` | `fast` / `standard` / `high` |
| `--concurrency` | `4` | 章节并行度 |
| `--max-cost-usd` | none | 超额自动暂停（带 checkpoint） |
| `--dry-run` | false | 仅产出 Pass 1，不翻译 |
| `--force-rerun` | false | 忽略 checkpoint，从头重跑 |
| `--ocr` | false | PDF 走 OCR |

### 上下文调参

| 参数 | 默认 |
| --- | --- |
| `--window.before` | 3 |
| `--window.after` | 2 |
| `--window.glossary-max` | 40 |
| `--window.token-budget` | 6000 |

### 风格参数

| 参数 | 说明 |
| --- | --- |
| `--register` | 强制覆盖自动检测（`academic-formal` 等） |
| `--quote-style` | `「」` / `“”` / `""` |
| `--punctuation` | `full` / `half` / `preserve` |

### 高级

| 参数 | 说明 |
| --- | --- |
| `--no-survey` | 跳过 Pass 1（不推荐；用已有 survey 时配合 `--survey-from`） |
| `--survey-from <dir>` | 复用已有 Pass 1 工件 |
| `--prompt-version <agent>=<v>` | 指定某 agent 的 prompt 版本 |
| `--config <file>` | 加载配置文件 |

## 配置文件

`~/.abi/config.yaml`（用户级） + `./abi.yaml`（项目级，优先级更高）：

```yaml
# v0.1：所有 LLM 都通过 OpenAI 兼容协议接入，靠 base_url 区分
llm:
  base_url: https://api.openai.com/v1   # 由 LLM_BASE_URL env 覆盖
  api_key_env: LLM_API_KEY
  model: gpt-4o-mini
  temperature: 0.2
  request_timeout_s: 60

observability:
  langfuse:
    enabled: true                       # 缺凭据时自动 no-op，不报错
    host: https://cloud.langfuse.com    # 自托管时换成你自己的 URL
    public_key_env: LANGFUSE_PUBLIC_KEY
    secret_key_env: LANGFUSE_SECRET_KEY
    upload_full_payload: false          # 默认仅上传 metadata + token usage

defaults:
  target: zh
  mode: [translated, annotated]
  quality: standard
  concurrency: 4

window:
  before: 3
  after: 2
  glossary_max: 40
  token_budget: 6000

style:
  quote_style: "「」"
  punctuation: full

cost:
  hard_cap_usd: 50           # 单 run 上限
  warn_at_usd: 10
```

### 常用兼容端点示例

```bash
# OpenAI 官方
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_API_KEY=$OPENAI_API_KEY

# DeepSeek
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_API_KEY=$DEEPSEEK_API_KEY

# 本地 Ollama（启动时加 OpenAI 兼容层）
export LLM_BASE_URL=http://localhost:11434/v1
export LLM_API_KEY=ollama   # 占位即可

# 自建 vLLM
export LLM_BASE_URL=http://gpu-host:8000/v1
export LLM_API_KEY=$INTERNAL_TOKEN
```

## 配置优先级

CLI 参数 > 项目 `./abi.yaml` > 用户 `~/.abi/config.yaml` > 内置默认值

所有配置最终合并为一个 `RunConfig`，写入 `manifest.json`，便于复现。

## Quality Preset

| preset | 行为 |
| --- | --- |
| `fast` | window.before=1, after=0, 关闭自审，模型可降级 |
| `standard` | 默认参数（见上） |
| `high` | window.before=5, after=3, 启用 `TranslationReviewer` 自审，max_retries=3 |

## 退出码

| code | 含义 |
| --- | --- |
| 0 | 成功，无 flag |
| 0 + warning | 成功但有 flagged 段（stderr 输出汇总） |
| 1 | 参数错误 |
| 2 | 输入文件解析失败 |
| 3 | LLM provider 不可用 |
| 4 | 成本上限触发，已 checkpoint |
| 5 | 其他运行时错误（详见 `events.jsonl`） |

## 进度显示

默认（TTY 下）：rich 进度条，显示 当前 pass / 章节进度 / 段落进度 / 累计 token 与 cost。

`--quiet`：仅输出错误。
`--json-events`：把 `events.jsonl` 镜像到 stdout，便于 CI/脚本消费。

## 示例

```bash
# 最简（默认走 $LLM_BASE_URL）
abi translate book.epub

# 显式指定端点 + 模型
abi translate book.epub --base-url https://api.deepseek.com/v1 --model deepseek-chat

# 高质量 + 多输出（v0.2 才支持 annotated / quality high）
abi translate book.txt -o ./out --mode translated,bilingual

# 仅概要
abi survey book.epub -o ./survey-only

# 本地离线（Ollama 兼容端点）
abi translate book.txt --base-url http://localhost:11434/v1 --model qwen2.5:32b

# 续跑
abi resume

# 编辑术语表后增量重跑（v0.2）
abi glossary edit ab12cd34
```
