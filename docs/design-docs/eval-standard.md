# ABI Evaluation 标准

> 一句话定位：ABI 不是「输入→输出」的黑盒，而是 28 态状态机驱动的 17 阶段 agentic 流水线。
> 因此「评测」必须分层——只评最终 EPUB 会漏掉**过程作弊**（门禁被绕过、某章质控其实并非
> 零问题）。本标准定义三个评测平面（L1 流程可信度 / L2 中间产物 + 逐章译文 / L3 最终产物）
> 与一条横切的系统指标线，给出每个指标的**产物来源、计算方式、判据、评测主体**。
>
> 前置阅读：[`agentic-pipeline.md`](./agentic-pipeline.md)（28 态流程权威总览）、
> [`langgraph-and-state-machine.md`](./langgraph-and-state-machine.md)（双层架构）、
> [`../QUALITY_SCORE.md`](../QUALITY_SCORE.md)（段落级评分子项，本标准对其在新架构下做了修正）。
>
> 本文取代已删除的 `eval-design.md`。旧文档描述的是 v0.1 重构前的评测载体
> （`abi translate` / `runs/<book_id>/<run_id>/` / 运行时 `TranslationUnit`），那些载体已随
> agentic 重构失效；其**方法学**（LLM-judge 的 likert/pairwise、A/B 槽位随机化、数据集
> adapter、段落软对齐）被搬进本文 L3。

---

## 0. 评测主体与总原则

每个指标标注评测主体：

- 🟢 **确定性**：纯函数，零额外 LLM 调用。优先级最高。
- 🟡 **LLM-as-judge**：必须可复算、记录 prompt 版本与模型；只用于成书层（L3）。
- 🔵 **人工**：仅用于黄金集标注与 judge 校准。

总原则：

1. **能确定性就不上 LLM**。L1 全确定性；L2 译文评分全确定性；LLM-judge 集中在 L3 成书层。
2. **独立重放裁决**。eval 进程独立 import `validators.validate()` / `validate_random_spotcheck()`
   重算门禁，而不是相信 `pipeline_state.json` 里 agent 记下的结果——这正是验证「不变量 3：
   agent 不能自判 PASS」是否被守住。
3. **可复现**。抽检 seed、状态轨迹、判分公式版本都要可复算；公式变更须递增版本号。

---

## 1. 三平面 + 横切线总览

| 平面 | 评什么 | 回答的问题 | 主要数据源 | 主体 |
| --- | --- | --- | --- | --- |
| **L1 流程可信度** | 状态机 / 门禁执行轨迹 | 流程是否被忠实、不可绕过地走完？ | `state/pipeline_state.json`、`events.jsonl`、`metrics.json` | 🟢 |
| **L2 中间产物 + 逐章译文** | 每阶段落盘工件 + 逐章译稿 | 研究/试译/术语/逐章译文/章节门禁本身够不够好？ | `metadata/`、`glossary/`、`qa/`、`chapters/` | 🟢 |
| **L3 最终产物** | EPUB + 成书译文 | 读者拿到的书好不好？比 baseline 强多少？ | `output/book.epub`、抽检、对比评测 | 🟢 裁决 + 🟡 judge |
| **横切 系统指标** | 成本/时延/可靠性/可复现 | 这次跑得贵不贵、稳不稳、能否复现？ | `metrics.json`、`events.jsonl`、`state.history` | 🟢 |

落地优先级：**L1 + 横切 →（同时）L2 译文 → L3**。L1 纯确定性、零成本、最先有回报。

---

## 2. L1 · 流程可信度（全确定性）

设想入口：`abi eval-trace <project_dir>` → 只读工件、独立重放 validator、写 `eval/<eval_id>/trace_report.json`。

| 指标 | 计算方式 | 判据 |
| --- | --- | --- |
| `gate_integrity` ⭐ | 对 `state.gates` 每条结果，用独立 import 的 `validate(project, produces)` / `validate_random_spotcheck(project)` 重算，与记录值比对 | 任一「state=PASS 但重算 FAIL」→ **CRITICAL**（违反不变量 3，整轮 eval FAIL） |
| `pipeline_completion` | 读 `status`；非 `DONE` 时记 `last_error` / `blocked_reason` 与停在哪态 | `status==DONE` 为 PASS |
| `path_conformance` | 检查 `history` 的状态跃迁是否为 `HAPPY_PATH` 的合法子序列（门禁 FAIL 回退到 `*_FAILED` / `REVISION_ROUTING_REQUIRED` 允许） | 出现非法跳态 = FAIL |
| `forbidden_violation` | 用工件交叉验证系统提示 Forbidden 清单。例：① 每个存在的 `chapters/final/{slug}.md` 必须有 PASS 的 `qa/gates/{slug}.gate.md`；② `pretranslation_report.md` 结论 PASS 的时间须早于首个 `chapters/translated/*.md` 落盘；③ `output/book.epub` 的 mtime 须晚于样章 PASS | 任一命中 = FAIL |
| `first_pass_rate` | 各门禁阶段 `events.jsonl` 中 `stage.end attempts==1 && ok` 的比例 | 监控 + 回归告警，不设硬门禁 |
| `retry_count` / `stage_attempts` | 从 `stage.end attempts` 聚合每阶段重试次数 | 同书重试数显著上升 → 告警 |
| `recursion_cap_rate` | `agent.run.capped` 占 `agent.run.*` 的比例 | 偏高 = 单元在某阶段不收敛，需改 prompt 或提 `max_iterations` |

> `gate_integrity` 是整个 eval 的命门：它独立证明「门禁不可被 agent 绕过」。L1 报告应能单独运行。

---

## 3. 横切 · 系统指标（全确定性）

直接从 `metrics.json` / `events.jsonl` / `state.history` 读取，无需额外调用：

| 指标 | 来源 | 用途 |
| --- | --- | --- |
| `cost_usd`、`tokens.{input,output,cached}` | `metrics.json` | 单位成本、回归告警 |
| `cost_per_1k_target_chars` | `cost_usd ÷ 译文字数（chapters/final）` | 跨书 / 跨模型可比的效率指标 |
| `duration_s`、各阶段时延 | `metrics.json` / `stage.start→stage.end` 时间差 | 时延回归 |
| `llm_calls` 分布 | `events.jsonl` 的 `agent.call` / `agent.run.*` | 调用结构画像 |
| `slim_call_compliance` | 07 阶段每次翻译调用的 input tokens 是否在单章上限内 | 超限说明 slim 约束（原文 + 5-8 条规则 + 命中术语）被破坏 |
| `budget_stop_rate` | `pipeline.budget` 事件出现频率 | 预算超限频率监控 |
| `reproducibility` | 固定抽检 seed 重跑，采样单元集合 + 状态轨迹是否一致 | 抽检与流程可复现 |

---

## 4. L2 · 中间产物 + 逐章译文质量

### 4.1 工件存在性 + 结构（确定性）

逐阶段「该检查的产物」与结构判据（全部可纯函数校验）：

| 阶段产物 | 结构判据 |
| --- | --- |
| `metadata/style_profile.md` | 覆盖语域 / 人称 / 标点 / 专名策略四要素 |
| `qa/pretranslation/pretranslation_report.md` | A/B/C/D 四变体齐全 + 含「为何 D 最适合正文」论证 + 结论 `result: PASS` |
| `glossary/terms.csv` | schema 完整（locked/preferred/avoid + 禁用正文写法）、≥1 术语行、无重复 / 冲突术语 |
| `glossary/style_guide.md` | 条目化、规模可被 slim 调用注入 |
| `qa/.../{slug}.control.md` | `_has_zero_issue_pass` 重算为真（5 字段最后一次取值全中零问题 PASS） |
| `qa/gates/{slug}.gate.md` | `_contains_pass`（`result: PASS`）且 `chapters/final/{slug}.md` 存在 |
| `preproduction/production_spec.md` | 版式 / 封面 / 前置页 / 署名策略齐全 |
| `preproduction/sample_book.epub` + `sample_review.md` | 样章三件套（lint+assets+epubcheck）离线重算通过 + `sample_review_status: PASS` |

### 4.2 逐章译文段落级评分（事后对齐，确定性）

新架构逐章翻译只产出纯 markdown 译文（`chapters/translated/{slug}.md`），**没有运行时
`TranslationUnit`**。因此段落评分必须**事后对齐**计算。

**段落对齐**（每章 `chapters/src/{slug}.md` ↔ `chapters/translated/{slug}.md`）：

1. 两侧按 `\n\s*\n` 切段，跳过标题块（`#` 开头）与零信息段（passthrough / 长度 < 30）。
2. `len(src) == len(tr)` → 位置对齐。
3. `|len(src) - len(tr)| ≤ 0.1 * len(src)` → 按段落长度做 Needleman-Wunsch 软对齐，未匹配段标 `align_failed`。
4. 差距更大 → 该章标 `chapter_align_failed`，仅给章级粗评。

**段落分公式**（相对旧 `QUALITY_SCORE.md` 删去 `schema_valid` 0.30 与 `llm_self_confidence`
0.10——这两项假设了已不存在的结构化逐段输出；权重重分到四个仍可纯函数计算的子项）：

```
para_score =
    0.35 * term_compliance        # = 1 - (违反 locked term 数 / 源段 locked term 总数)，分母0取1
  + 0.25 * length_ratio_ok        # 按语言对区间平滑（en→zh 0.22–0.55，见 QUALITY_SCORE.md 表）
  + 0.20 * anchor_preservation    # = 译文命中 anchor 数 / 源段 anchor 数（数字/年份/专名/URL/引号串）
  + 0.20 * no_refusal_no_residue  # 无「I cannot/无法翻译/as an AI」且无源语言字符大段残留 → 1
```

**章节聚合**：`score_avg`、`score_p10`、flag 计数（`term_drift` / `length_ratio_outlier` /
`untranslated_residue` / `refusal_detected` / `align_failed`）、`completeness`（每源段都有非空
译文；翻译失败时为空串而非回填原文，故能立即抓到）、`term_consistency`（章内同一术语是否始终
译为 glossary 目标词）。可选 🟡 `style_drift`（仅作监控，用 embedding 比对章间语气向量）。

| 判据 | 阈值 |
| --- | --- |
| `completeness` | 漏译 / 空译 = 该章 **硬 FAIL** |
| `chapter score_avg` | 设回归基线；降幅 > 0.05 阻断 PR |
| flag 计数 | 监控 + 输出 Top-N 离群段清单（带 `unit_id` + 摘录） |

> 本节是 L2 重头。LLM-judge **不**在 L2 使用（控成本）；文采 / 语境类判断集中到 L3 成书层。

---

## 5. L3 · 最终产物质量

### 5.1 EPUB 工程合规（确定性硬门禁）

| 产物 | 判据 |
| --- | --- |
| `output/publication_lint.json` | 离线重算：无本机绝对路径 / mojibake / BOM、围栏配平、目标语排版，`ok==true && hard_errors==0` |
| `output/asset_manifest_check.json` | 图片 / 资源本地存在且被引用，`ok && hard_errors==0` |
| `output/book.epub` | EPUBCheck（Java jar）0 fatal / 0 error |
| `book-info.xhtml` / metadata | 署名 / 版权 /（私人自用）边界声明符合 policy（正则校验） |

### 5.2 抽检卓越线（确定性裁决 + 🟡 judge 输入）

离线重算 `validate_random_spotcheck`：avg≥92、min≥88、单项≥80、无 P0/P1/P2、
`release_confidence≥0.80`、当前 run ≥2 连续 PASS 轮；双独立评审 `reviews/agent_a|b/review.md`
均 PASS（不同 thread_id + seed，保证独立性）。

### 5.3 成书对比评测（🟡 LLM-as-judge，救活旧 eval-design 方法学）

ABI 成书译文 vs **baseline**（整书源文按固定大块塞进单条 prompt 一次性翻译再拼接）vs
**人工参考**（仅当数据集自带参考译文时）。

| 维度 | 提示要点 | 输出 |
| --- | --- | --- |
| `adequacy` | 是否传达源文全部关键信息 | Likert 1-5 |
| `fluency` | 目标语是否自然、合语法 | Likert 1-5 |
| `coherence` | 给定前后各 2 段，上下文是否连贯 | Likert 1-5 |
| `style` | 是否匹配预期语域 | Likert 1-5 |
| `pairwise` | 给出全部系统译文（**A/B/C 槽位随机化消除位置偏置**），选偏好侧 + 理由 | 胜率 |

实现要点：judge 走 `LLMRouter.invoke_structured`（自动 Langfuse trace + `BudgetGate` 预算上
限 + 结构化 pydantic 输出），judge 模型经 `--judge-model` 用 `model_override` 换更强模型而不动
其余配置；prompt 不透露「哪侧是 ABI」，A/B/C 槽位随机化，对应关系只记在内部 alignment 里；
数据集 adapter（**WMT24++** 为主，见 §6.5）将带参考的数据集物化为按 `document_id` 分章的
`.txt` 走标准 ingest，从而 1:1 段落对齐。

---

## 6. 聚合、报告与回归

### 6.1 统一报告

```
eval/<eval_id>/
├── manifest.json          # 被评 project、git sha、模型、公式版本、seed
├── trace_report.json      # L1 流程可信度 + 横切系统指标
├── chapters.jsonl         # L2 逐章段落评分聚合
├── artifacts.json         # L2 工件存在性 + 结构校验
├── final.json             # L3 EPUB 合规 + 抽检卓越线重算
├── judge/                 # L3 成书对比（likert.jsonl + pairwise.jsonl），仅对比模式
├── report.json            # 机器可读总览
└── report.md              # 人类可读总览
```

`report.md` 顶层给四维度各一个 `PASS / WARN / FAIL` + roll-up `release_recommendation`。

### 6.2 硬门禁划分

- **硬门禁**：L1 全部 + L2 工件结构 + L2 `completeness` + L3 确定性项（EPUB 合规、抽检卓越线重算）。
- **卓越线 + 监控**：L3 LLM-judge 维度、L2 `score_avg` 回归基线、所有系统指标。

### 6.3 双黄金集回归

> 核心区分：**业界数据集做「校准」，自建集做「回归闸门」**。两者不可混用——数据集的参考
> 译文不是你的定稿、且多受版权约束（见 §6.5），不能充当「我们认定的正确答案」这一回归基准。

**① 流程黄金集（护 L1 不变量）**

- 用极小的公版书 fixture（仓库已有 `tests/fixtures/short_book.txt`、`the_communist_manifesto.txt`）。
- CI 里用 **stub / mock 的 agent runtime**（不调真 LLM，避免非确定 + 成本），让假 runtime
  按脚本写出 canned 工件，测的是 orchestrator + `validate()` + 状态机的接线。
- 冻结期望轨迹到 `tests/golden/process/<fixture>/expected_trace.json`：`HAPPY_PATH` 跃迁序列、
  必须 PASS 的门禁名集合、关键工件存在性。**不**记录任何 LLM 文本。
- 真 LLM 端到端作为周期性 smoke（不阻塞 PR），只断言能到 `DONE` + `gate_integrity` 通过。

**② 译文黄金集（护 L2 公式 + 译文质量回归）**

按新架构（无 `TranslationUnit`、事后对齐）重建，三类条目：

| 类型 | 内容 | 作用 |
| --- | --- | --- |
| 参考型 | `source` + 版权干净的 `reference` + `target_lang` + 期望分区间 | 校准公式阈值 / 对标 judge |
| 快照型 | `source` + 你的人工定稿 `accepted` + 该 formula 版本下记录的 `para_score` | 回归闸门：分降 > 0.05 阻断 PR |
| 负例型 | `source` + 已知劣质译文 + 期望低分 / 期望命中的 flag | 断言公式真能抓 `term_drift` / `length_ratio_outlier` / `residue` |

构建纪律：

- **按现象分层，不纯随机**：每个出货语言方向覆盖文学叙事 / 学术可读 / 对话 / 术语密集 /
  anchor 密集 / 图表注释。起步每方向 30–60 段。
- **许可纪律**（呼应 `AGENTS.md`）：参考译文只能来自公版published译本（源与译都过版权期）/
  你自己拥有的定稿 / 研究许可数据集（仅内部用）。**绝不**用现代受版权译本。
- **格式**：`tests/quality/golden/<source-target>/*.jsonl`，机器可读；期望分按 formula 版本号冻结。

### 6.4 公式版本化

判分公式（L2 段落分、L3 judge prompt）变更必须：递增版本号 + 重算所有历史 eval 报告 + 在
CHANGELOG 说明。版本号写入 `manifest.json`，保证跨 run 可比。

### 6.5 校准数据集（业界，许可感知）

数据集用于**校准**（L2 `length_ratio` 区间、judge 与人类相关性）和 **ABI vs baseline 对比基准**，
不直接做回归黄金集。按对 ABI 的适配度排序：

| 数据集 | 方向 | 粒度 | 体裁 | 许可 | 用途 |
| --- | --- | --- | --- | --- | --- |
| **WMT24++**（`google/wmt24pp`） | en→**55** 语言 | 段落/segment | literary/news/social/speech，**post-edit 参考** | 研究用（核对条款） | **主力**：覆盖 en→{zh,ja,es…}；按 `domain` 取 literary 子集做 judge 校准 + 对比基准 |
| **PAR3**（katherinethai/par3） | 19 源语言→**en** | 段落 | 公版文学小说，2–5 人工译本 | 公版源 + 研究用译文（**勿 republish**） | 文学 X→en 段落级校准；注意 shuffled/partial，仅内部 |
| **FLORES+**（`openlanguagedata/flores_plus`） | en→**200+** | 句子 | Wikinews/Wikijunior/Wikivoyage（非虚构） | **CC BY-SA 4.0** ✅ | 广覆盖冷启动 smoke + 低资源方向 `length_ratio` 初校准；许可最干净可公开 |
| News-Commentary（Helsinki-NLP） | 多向 | 句/篇 | 学术可读评论 | 语料许可 | academic-accessible 体裁校准 |
| OPUS Books / Tatoeba | 多向 | 句 | 公版书摘 / 日常句 | 多为 CC（逐个核对） | 补充平行句对、低成本 smoke |

> 字段映射（WMT24++）：`source`（英文源）、`target`（post-edit，**推荐做默认参考**）、
> `original_target`（原参考）、`domain`、`document_id`、`segment_id`、`is_bad_source`（为 true
> 的 segment 应剔除）。`COMET / BLEURT / MetricX` 是*指标*非数据集，有参考时可作 L3 弱信号——
> 但 PAR3 论文指出传统指标在文学域与人类偏好不相关，故只与 judge 互补、不单独裁决。

---

## 7. 落地顺序

1. **L1 + 横切**：`abi eval-trace`，纯确定性、零 LLM 成本，立刻能抓「过程作弊」与成本回归。
2. **L2 逐章译文**：实现段落对齐 + 段落分公式，接 `chapters/translated/`，加章节聚合与黄金集。
3. **L3**：EPUB 合规 + 抽检离线重算（确定性）→ 成书对比 LLM-judge（沿用旧 eval-design 方法学）。

---

## 8. 相关源码入口

| 关注点 | 文件 |
| --- | --- |
| 28 态状态机 / HAPPY_PATH | `src/abi/project/state.py` |
| 确定性门禁（L1 重放对象） | `src/abi/stages/validators.py` |
| 抽检卓越线 validator（L3 重放对象） | `src/abi/qa/validator.py` |
| 分层随机采样器 / 审计单元 | `src/abi/qa/sampler.py`、`src/abi/qa/units.py` |
| 成本 / token / 段落 metrics | `src/abi/providers/observability/events.py`（`MetricsAggregator`） |
| 事件流 | `src/abi/providers/observability/events.py`（`EventLogger`） |
| 段落分子项与阈值表 | `docs/QUALITY_SCORE.md` |
| 工件目录合约 | `src/abi/project/layout.py`（`BookProject`） |
