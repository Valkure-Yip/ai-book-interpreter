# DESIGN.md — 架构不变量与品味规则

> 本文件是**机械可执行规则**的家。每一条都对应一个 linter / 类型检查 / 结构测试。
> 违反这里的任何一条都会让 CI 红灯。
>
> "在低吞吐量环境中，这样做是不负责任的。而在这里，这通常是正确的选择。" — 我们的反面：**正因为 LLM 输出多变，越要把不变量钉死**。

## 1. 分层与依赖（强制）

### 规则 D1: 单向依赖

```
types → config → ir → survey → translate → assemble → runtime → cli
providers ← 任何业务层（但 providers 不依赖业务）
```

**Lint**：`tools/lint/layered_imports.py` 解析 import 图，违反时报错并指出修复方法。

### 规则 D2: 业务层不直接 import SDK

`openai`, `anthropic`, `ebooklib`, `fitz` 等只能在 `src/providers/**` 中 import。

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

### 规则 D10: Prompt 有版本

`src/prompts/<agent>/v<N>.j2` + `current` 符号链接。
没有 `current` 链接的 agent 目录视为损坏。

### 规则 D11: Prompt 渲染测试

每个 agent 至少 3 个 fixture 测试 prompt 渲染输出字节级稳定。
**Test**：`tests/prompts/test_render.py`。

## 6. 输出与装配

### 规则 D12: 装配前 lint

Pass 3 前必须通过：
- `glossary_lint`：所有 source 中出现的 locked term 在译文中按 `target` 出现
- `anchor_lint`：所有 anchor 解析成功
- `markdown_lint`：`markdown-it` 解析无错
- `mermaid_lint`：mermaid 代码块语法正确

违反 lint 时仍可产出 `*.draft.md`（用于调试），但**不**产出非 draft 版本。

### 规则 D13: 段落-译文对应完整

每个源段落要么有 `TranslationUnit`，要么有显式 `SkipRecord`。装配时缺一即失败。

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

任何 LLM 调用前必须经过 token 估算 + 预算 gate。
超预算时按 `sliding-window.md` 的 trim_order 裁剪，仍超则 raise。

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
