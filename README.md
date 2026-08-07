# AI Book Interpreter

> 一个自包含的自主翻译 agent：输入公版或已授权的 `txt` / `epub`，由受约束的动态
> `Planner + PolicyEngine + durable Action loop` 组织研究、试译、逐章翻译、质量审查、EPUB 构建和发布，
> 输出版本化、经确定性门禁的 EPUB 译本。

ABI 不把 LLM 当作流程状态机。模型负责提出短期计划和执行开放式工作；代码负责资格计算、权限、预算、
工件清单、质量门禁、提交与恢复。`state/run.db` 中的 RunLedger 是唯一业务真相。

## 核心特性

| 特性 | 当前行为 |
| --- | --- |
| **受约束动态编排** | Planner 每轮提出未来 1–5 个 Action；PolicyEngine 只批准 Registry 中当前 eligible 的能力 |
| **精确修复** | QA 缺陷映射为 typed repair，按受影响章节或问题族创建新 plan/action/staging，不重跑整条固定 happy path |
| **权限化 Action harness** | 每个 Action 只获得声明过的 skills、读写路径和工具；读可并行，写与其他副作用串行 |
| **可证明的工件提交** | executor 只写 attempt staging；exact manifest、validator、promotion intents 和 canonical postcheck 全部通过后才提交成功 |
| **章节质量链** | `translated → controlled → final`，并执行忠实度、可读性/意象、术语、独立双审与分层随机抽检 |
| **EPUB 与发布** | publication lint、asset manifest、EPUBCheck 通过后生成 EPUB，并写入版本化 `output/release/` |
| **可靠恢复** | `abi resume` 先对账 RunLedger、receipt、intent 与文件系统；无法证明的结果 fail closed，不猜测成功 |
| **可观测与成本上限** | 所有 LLM 调用经 Langfuse v4、本地 `events.jsonl` 和 BudgetGate；缺 Langfuse 凭据时安全降级 |
| **人工断点** | HITL 使用不可变 pause receipt 与 append-only continuation；integrity block 必须携带证据显式 `unblock` |

架构图和完整协议见 [ARCHITECTURE.md](./ARCHITECTURE.md) 与
[受约束的动态 Agent 编排](./docs/design-docs/dynamic-agent-orchestration.md)。

## 环境要求

| 依赖 | 要求 | 说明 |
| --- | --- | --- |
| Python | `>=3.11` | 推荐使用 `uv` 管理虚拟环境 |
| LLM 端点 | OpenAI-compatible | 通过 `base_url` 与模型配置切换服务 |
| JRE | 可执行的 `java` | EPUBCheck 硬门禁需要；可安装 `epub` extra 或设置 `ABI_EPUBCHECK_JAR` |
| Langfuse | v4，可选 | 缺凭据或不可达时不阻塞业务流程 |

## 安装

```bash
git clone https://github.com/<your-org>/ai-book-interpreter.git
cd ai-book-interpreter

uv venv
source .venv/bin/activate
uv pip install -e ".[dev,epub]"

abi --help
```

也可以用 Python 3.11+ 的 `venv` 与 `pip install -e ".[dev,epub]"`。

## 配置

复制示例环境文件并填入 LLM 配置：

```bash
cp .env.example .env
```

最小配置：

```bash
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
```

Langfuse 可选配置：

```bash
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_FULL_PAYLOAD=0  # 0=正文脱敏，1=上传完整 prompt/completion
```

完整环境变量见 [.env.example](./.env.example)，CLI 配置优先级与安全停止语义见
[CLI and Configuration](./docs/product-specs/cli-and-config.md)。

## 快速开始

支持本地路径或 URL 的 TXT / EPUB：

```bash
# 创建书籍工程，并运行到 COMPLETED 或一个可检查的安全停止点
abi make-book tests/fixtures/short_book.txt \
  --source-target en-zh-Hans \
  --title short-book

# 中断后恢复同一个 durable run
abi resume books/zh-Hans/0001_short-book

# 查看 authoritative facts、开放 incident 与下一条安全恢复命令
abi inspect books/zh-Hans/0001_short-book

# 提交一个公开 HITL interrupt 的决定
abi approve books/zh-Hans/0001_short-book \
  <interrupt-id> \
  --decision approve

# 取消尚未完成的 run
abi cancel books/zh-Hans/0001_short-book
```

ABI 不提供 `--until`、`abi state` 或固定阶段跳转。Planner 可以调整工作顺序，但不能绕过依赖、门禁、
预算与权限。`inspect` 会在预算暂停、HITL 或 integrity block 时给出精确的后续命令。

## 书籍工程

工程默认位于 `books/{target}/{NNNN}_{slug}/`。关键内容包括：

```text
source/                         # 受控原文、目录与 source manifest
metadata/                       # 版权、全局/本书研究与文体画像
glossary/                       # style guide 与术语表
chapters/src/                   # 拆分后的原文章节
chapters/translated/            # 不可变初译
chapters/controlled/            # 章控修订
chapters/final/                 # 通过章节门禁的最终译文
qa/                             # 章控、忠实度、可读性、意象、术语证据
reviews/                        # 独立双审与分层随机抽检
preproduction/                  # 制作规格与样书
output/book.epub                # 通过构建门禁的 EPUB
output/release/                 # 版本化发布物与 release state
retrospective/                  # 运行复盘与模板改进建议
state/run.db                    # 唯一业务真相
state/graph_checkpoints.sqlite  # 宏观 LangGraph 运行时游标
state/action_checkpoints.sqlite # Action agent/HITL 运行时游标
events.jsonl                    # 可重建的本地事件投影
```

Action 输出先进入 `state/staging/{action_id}/{attempt}/`；只有门禁和提交协议全部通过后才进入上述
canonical 路径。

## 当前验证状态

- `short_book.txt` 已真实跑到 `COMPLETED`，产出 `book_v0.0.1.epub`；独立双审、随机抽检和
  EPUBCheck 均通过。
- 当前自动化基线为 `777 passed`，Ruff 全仓库通过。
- 《共产党宣言》长书验证已覆盖真实 LLM、Langfuse 与多章节链路，但一次人为中断留下
  `RUNNING` 且缺 outcome receipt 的 attempt。系统按设计以 `artifact_bundle_conflict` 阻断；这证明了
  fail-closed 恢复边界，不等同于长书全流程完成。

详见动态编排设计的[实现与验证记录](./docs/design-docs/dynamic-agent-orchestration.md#19-依赖与文档变化)。

## 文档地图

- [AGENTS.md](./AGENTS.md)：贡献者与 agent 的入口地图
- [ARCHITECTURE.md](./ARCHITECTURE.md)：系统架构、流程图与分层依赖
- [docs/DESIGN.md](./docs/DESIGN.md)：强制不变量与 linter 规则
- [docs/RELIABILITY.md](./docs/RELIABILITY.md)：receipt、重试、恢复、HITL 与 unblock
- [docs/QUALITY_SCORE.md](./docs/QUALITY_SCORE.md)：当前质量证据与门禁层级
- [docs/SECURITY.md](./docs/SECURITY.md)：凭据、正文、路径与追踪安全
- [docs/design-docs/index.md](./docs/design-docs/index.md)：详细设计索引
- [docs/product-specs/index.md](./docs/product-specs/index.md)：用户可见行为
- [docs/design-docs/eval-standard.md](./docs/design-docs/eval-standard.md)：L1/L2/L3 eval 标准

## License

MIT
