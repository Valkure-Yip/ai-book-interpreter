# CLI and Configuration

## 1. Lifecycle commands

```text
abi make-book SOURCE [options]
abi resume PROJECT_ROOT [options]
abi inspect PROJECT_ROOT
abi approve PROJECT_ROOT INTERRUPT_ID --decision approve|reject [--feedback TEXT]
abi unblock PROJECT_ROOT --reason TEXT --evidence-ref REF ... [recovery options]
abi cancel PROJECT_ROOT
```

| 命令 | 用户可见行为 |
| --- | --- |
| `make-book` | scaffold 书籍工程、放置 txt/epub 输入、创建唯一 durable run、驱动到 terminal 或 safe stop |
| `resume` | 从 `state/run.db` 恢复该工程唯一 run；零/多 run 或 identity 不一致 fail closed |
| `inspect` | 只读 run/plan/Action/receipt/gate/intent/incident/budget 与当前 public HITL identity，给出下一安全操作 |
| `approve` | 对 exact public interrupt 提交 ordered decision/feedback，走 append-only HITL continuation |
| `unblock` | 恢复 budget pause，或以人工 evidence 替换 integrity-blocked Action；不处理 semantic repair |
| `cancel` | 幂等取消非 completed run；不重开 terminal run |

没有宏观阶段参数或“运行到某一步”的兼容入口。Planner 根据 durable facts 选择 eligible capability；用户
通过 `inspect` 查看当前事实和可复制的 recovery command。

## 2. `make-book`

```text
abi make-book SOURCE \
  --source-target en-zh-Hans \
  [--title SLUG] [--books-root books] \
  [--mode public_domain|licensed|private_use] [--profile NAME] \
  [--base-url URL] [--model MODEL] [--max-cost-usd N] [--config FILE]
```

输入仅支持实现声明的 txt/epub path 或 URL。`private_use` 写入 `{books_root}/private/`。命令创建书籍工程、
`state/run.db`、attempt staging 和 checkpoint 父目录；同一工程只拥有一个 business run。

## 3. HITL approve

`inspect` 显示 interrupt ID、action/attempt、claim status、continuation sequence 与完整 approve command。
`approve` 的 decision/feedback 顺序必须和 public reviews 一一对应，且只接受该 review 声明的 decision。
命令不会改写初始 Paused receipt，也不会直接提交成功；continuation 交回 Reconciler/Committer。

## 4. Integrity unblock

```text
abi unblock PROJECT_ROOT \
  --reason "operator resolved canonical conflict" \
  --evidence-ref ticket-42 \
  --source-action ACTION_ID \
  --resolved-canonical path/to/file:removed

abi unblock PROJECT_ROOT \
  --reason "selected verified canonical artifact" \
  --evidence-ref ticket-43 \
  --source-action ACTION_ID \
  --resolved-canonical path/to/file:selected:SHA256
```

每个 conflict path 必须 exact coverage。成功后旧 Action/attempt/receipt/intent 保持不变，创建 new plan、
Action ID 与 staging。budget-only unblock 只需 reason/evidence，不带 source 或 canonical resolution。

## 5. Eval commands

```text
abi eval trace PROJECT_ROOT
abi eval book PROJECT_ROOT [--source-lang en] [--target-lang zh-Hans]
abi eval calibrate --dataset DATASET [--min-samples N]
```

`trace` 从 RunLedger typed reads 重放 L1 gate/policy conformance；`book` 汇总 L1/L2/L3。eval 不读取
checkpoint 私有表或 raw SQLite。

## 6. Configuration

优先级：CLI overrides > `--config`/项目配置 > 用户配置 > 内置默认值。边界解析为 frozen `RunConfig`。

```yaml
llm:
  base_url: https://api.openai.com/v1
  api_key_env: LLM_API_KEY
  model: gpt-4o-mini
  temperature: 0.2
  request_timeout_s: 240

langfuse:
  enabled: true
  host: https://cloud.langfuse.com
  public_key_env: LANGFUSE_PUBLIC_KEY
  secret_key_env: LANGFUSE_SECRET_KEY
  upload_full_payload: false

cost:
  hard_cap_usd: 50
  warn_at_usd: 10
```

CLI 会加载当前目录和仓库根的 `.env`，且不会覆盖已经存在的进程环境变量。API keys 只从环境变量读取，
不写入工程、events、checkpoint 或 report。`--max-cost-usd` 只覆盖本次运行预算；hard cap 产生 durable
budget pause，可提高额度后显式恢复。

## 7. 退出与安全停止

CLI 输出 run ID、status、blocked reason 与工程路径。参数/输入/provider/运行时错误使用非零退出；预算、
HITL 与 integrity stop 是可检查的 durable 状态，不通过删除 checkpoint 或改写 projection 恢复。
