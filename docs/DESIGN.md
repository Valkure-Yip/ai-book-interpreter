# DESIGN.md — 架构不变量与品味规则

> 本文件是**机械可执行规则**的家。每一条都对应一个 linter / 类型检查 / 结构测试。
> 违反这里的任何一条都会让 CI 红灯。
>
> "在低吞吐量环境中，这样做是不负责任的。而在这里，这通常是正确的选择。" — 我们的反面：**正因为 LLM 输出多变，越要把不变量钉死**。

## 1. 分层与依赖（强制）

### 规则 D1: 单向依赖

```
types → config → ir → project → epub → qa → release → tools → actions → planning → orchestrator → cli
providers (llm, agent_runtime, observability) ← 任何业务层（但 providers 不依赖业务）
```

- `project`：书籍工程目录、artifact promotion 与 `state/run.db` RunLedger 合约。
- `epub` / `qa` / `release`：确定性产物层（EPUB 构建、出版 lint、随机抽检、版本发布），不调用 LLM，只被 `tools` 调用。
- `tools`：attempt-aware、默认拒绝的 scoped 工具带，是业务层与 agent runtime 的桥梁。
- `actions`：注册 capability、schema、effects、权限、validator、retry 与 read/write sets。
- `planning`：从完整 policy snapshot 计算 eligibility，验证 Planner 的短期 `PlanPatch` 并调度无冲突 Action。
- `orchestrator`：controller、dispatcher、reconciler、committer 与 lifecycle；不拥有绕过 ledger 的状态写入口。

**Lint**：`tools/lint/layered_imports.py` 解析 import 图，违反时报错并指出修复方法。

### 规则 D2: 业务层不直接 import SDK

`openai`, `anthropic`, `ebooklib`, `fitz`, `langchain*`, `langgraph*`, `langfuse*` 等只能在 `src/abi/providers/**` 中 import。
其中 agent 运行时（LangGraph tool-calling loop）集中在 `providers/agent_runtime`；业务层只通过 `providers.llm` 的 `LLMRouter` 或 `providers.agent_runtime` 的 agent 接口触达模型，所有 LLM 调用因此自动接入 Langfuse trace + `events.jsonl` + `BudgetGate` 成本上限。

**Lint**：`tools/lint/no_direct_sdk.py`，白名单驱动。

### 规则 D3: 副作用集中

`types/` 与 `config/` 模块必须是纯：不读文件、不读时钟、不调网络、不读环境变量。

**Lint**：`tools/lint/purity.py` 检查 AST，匹配 `open`, `requests`, `os.environ`, `datetime.now` 等模式。

## 2. 数据边界

### 规则 D4: 边界处解析数据形状

所有外部输入必须在进入业务层前被 pydantic 解析：
- LLM 响应 → `*Output` schema
- 文件读取 → `Book*` 模型
- CLI 参数 → `RunConfig`
- 配置文件 → `AppConfig`

不允许 `dict` / `Any` 跨越 `providers → 业务层` 边界。

**Lint**：`tools/lint/parse_at_boundary.py` 检查 `providers/**` 模块的 public 函数签名返回类型。

### 规则 D5: 不可变模型

所有 pydantic 模型 `model_config = ConfigDict(frozen=True)`。变更走构造新对象的路径。

## 3. 标识符

### 规则 D6: 段落 ID 算法不变

`paragraph_id(text, position)` 实现见 `data-model.md`。**任何变更都是破坏性变更**，须递增 schema_version 并提供 migration。

**测试**：`tests/test_paragraph_id_stability.py` 含 100 个 fixture，CI 守护。

### 规则 D7: 命名约定

- 函数/变量：`snake_case`
- 类/pydantic 模型：`PascalCase`
- 常量：`UPPER_SNAKE`
- 文件：`kebab-case.md` for 文档，`snake_case.py` for 代码

## 4. 日志与可观测性

### 规则 D8: 结构化日志

禁止：
```python
print(x)
logging.info(f"translated paragraph {pid}")
```

允许：
```python
log.event("paragraph.translated", paragraph_id=pid, tokens=tu, cost_usd=c)
```

**Lint**：`tools/lint/no_print.py` 禁 `print`、`logging.*(f-string)`。

### 规则 D9: LLM 调用必须经事件管道

每次 `providers.llm.chat()` 内部必落 `agent.call` 事件。
**Lint**：`tools/lint/llm_calls_traced.py` 走 AST 检查所有调用点。

## 5. Prompt 工程

### 规则 D10: Planner 与 Action prompt 分离

Planner 只接收压缩的 `planner_snapshot`、eligible capabilities 与预算，输出强类型 `PlanPatch`；
Action harness 只加载该 Action 的输入、skills 与 scoped tools。翻译 Action 仍只得到原文、5–8 条
文体规则与命中术语，禁止混入 QA、EPUB、release 或宏观 transition 指令。

### 规则 D11: Prompt 不授权业务事实

Prompt、模型输出、LangGraph checkpoint 和 `events.jsonl` 都不能写 run/Action/gate/success。
业务 transition 只由 PolicyEngine、RunLedger、Reconciler 或 Committer 的确定性代码完成。

## 6. 输出与装配

### 规则 D12: 构建前门禁

全书 EPUB 构建前必须通过确定性门禁（`src/abi/epub/`）：
- `publication_lint`：无本机绝对路径 / mojibake / BOM、围栏配平、目标语言排版基本检查
- `asset_manifest_check`：所有被引用图片均为本地且存在
- `epubcheck`：EPUBCheck（Java jar）无 fatal / error

任一 FAIL 即按 Registry 映射进入 semantic repair，或 fail closed 为 integrity block；PASS gate receipt
必须与完整 promotion intent 集同事务持久化，并在 unified canonical postcheck 后才能提交 success。

### 规则 D13: 章节-译文对应完整

`chapters/src/` 中每个章节都必须依次产生不可变的 `chapters/translated/` 初译和
`chapters/controlled/` 章控修订，并在章节门禁 PASS 后进入 `chapters/final/`；后继 Action 不得覆盖
已提交 canonical 版本。缺一即 validator 失败，不得授权构建或 release Action。

## 7. 文档与代码同步

### 规则 D14: 文档新鲜度

`docs/generated/**` 由代码生成，不可手改。
CI 中跑 `tools/codegen/dump_*.py`，diff 非空即失败。

### 规则 D15: AGENTS.md 短

`AGENTS.md` 不超过 150 行；`ARCHITECTURE.md` 不超过 300 行。
**Lint**：`tools/lint/doc_size.py`。

### 规则 D16: 入口表完整

`AGENTS.md` 中"我最常需要的入口"表必须覆盖所有顶级文档（`docs/*.md` + `docs/*/index.md`）。
**Lint**：`tools/lint/agents_index.py`。

## 8. 测试

### 规则 D17: 类型完整

`mypy --strict` 通过。

### 规则 D18: 关键路径覆盖

`paragraph_id`、`build_context`、所有 renderer、所有 ID 算法：单元测试覆盖率 100%。
其余目标：≥ 80%。

### 规则 D19: 端到端测试

`tests/e2e/` 包含至少 3 本小书（fixture）：英→中、中→英、含数学公式的技术书。
每次 PR 必跑。

## 9. 性能与成本

### 规则 D20: Token 预算有上限

任何 LLM 调用前必须经过 token 估算 + 预算 gate（`providers/llm/budget.py` 的 `BudgetGate`）。
超预算时按 gate 的裁剪/降级策略处理，仍超则 raise。

### 规则 D21: 成本 hard cap 默认开启

`config.cost.hard_cap_usd` 默认值非 None；触发时优雅停（保存 checkpoint）。

## 10. 品味（次要但有立场）

### 规则 D22: 不要"AI 残渣"

参考 Codex 团队"垃圾回收"实践：
- 同一概念有 2 个实现 → 合并
- 死代码、未引用的 prompt 版本（除最近 3 个） → 删
- 注释开头"This function..." 而函数名已自明 → 删
- pydantic 字段无 docstring 时，模型 docstring 必须解释字段语义

由 `tools/garden/` 中的智能体每周扫描 + 自动 PR。

### 规则 D23: 错误信息含修复指令

所有自定义 lint / 异常类，message 必须包含 "如何修复" 的提示。
反例：`"invalid paragraph id"`。
正例：`"invalid paragraph_id 'xyz' for paragraph at position 47. paragraph_id must be sha1(normalize(text))[:10]+'-'+f'{position:06d}'. Re-run pass 0 to regenerate, or call ir.recompute_ids()."`

## 与 OpenAI 工程规范的对应

| OpenAI 原则 | 本文件中的规则 |
| --- | --- |
| Parse, don't validate | D4 |
| 严格边界 | D1, D2, D3 |
| 不变量机械执行 | D6, D8, D9, D12, D13 |
| 仓库即记录系统 | D14, D15, D16 |
| 错误信息含修复指令 | D23 |
| 垃圾回收 | D22 |
