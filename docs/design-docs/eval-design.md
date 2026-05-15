# Evaluation pipeline — design

## 目标

衡量 ABI 多 pass 翻译相对于"原文整段塞进上下文一次性翻译"baseline 的质量差异，给出可比较、可解释、可复现的多维评测报告。

非目标：取代人工质检；给出绝对质量分（仅给出 ABI vs baseline 的相对差）。

## 评测对象

- **A 系统（ABI）**：本仓库 `abi translate` 的产物（per-paragraph `TranslationUnit`，含 confidence、flags、glossary 合规等）。
- **B 系统（Baseline）**：把全书源文直接塞进单条 prompt，要求模型一次性输出译文。当书超出上下文窗口时，按固定大块（默认 50K input tokens / 块）顺序翻译并拼接。除分块外不做任何 ABI 风格的预处理（无 survey、无 glossary、无 sliding window、无 anchor 保留指令）。
- **共同输入**：同一份源文件、同一个目标语言、同一个 LLM 模型（默认与 `LLM_MODEL` 一致；可通过 `--judge-model` 显式指定 judge 用更强模型）。

## 维度

### 机械指标（确定性，零额外 LLM 调用）

| 维度 | 说明 | 计算方式 |
| --- | --- | --- |
| `glossary_compliance` | 译文是否使用 ABI 锁定术语的官方目标语 | 扫描每个样本译文，统计核心术语命中 / 命中正确 |
| `length_ratio` | 译文 / 源文字符比是否落在合理区间 | 与 `translate/validator.py` 相同区间（en→zh: 0.22-0.55） |
| `anchor_preservation` | 数字、年份、专名、URL、引号内字符串保留率 | 抽取源文中的正则 anchors → 检查译文是否完整出现 |
| `completeness` | 段落级别是否漏译 | baseline 段落与源段落对齐失败 / 译文为空 / passthrough |

### LLM-as-Judge（每样本一次或两次调用）

| 维度 | 提示 | 输出 |
| --- | --- | --- |
| `adequacy` | "Does the translation convey all key information of the source?" | Likert 1-5 |
| `fluency` | "Is the translation natural and grammatical in the target language?" | Likert 1-5 |
| `coherence` | "Does the translation read coherently in context (prev/next 2 paragraphs given)?" | Likert 1-5 |
| `style` | "Does the translation match the expected register (academic-formal here)?" | Likert 1-5 |
| `pairwise` | 同时给出 A、B 两条译文（**随机化 A/B 标签消除位置偏置**），要求 judge 选偏好侧并给理由 | `prefer ∈ {A, B, tie}` + 简短 rationale |

Judge 在同一个 prompt 中同时打 A 和 B 两套 Likert 分数，避免分别打分时的"绝对值漂移"。Pairwise 单独一次调用，A/B 顺序随机翻转。

## 采样策略

按段落总数与章节数确定样本量：

- 默认 `--samples 30`
- 按章节段落数比例分层抽取
- 每章强制包含首段 + 末段（覆盖章节边界）
- 跳过 `passthrough` / 长度 < 30 char 的零信息段

## 段落对齐

baseline 是单条长文本输出，需要回到与 ABI 等价的段落粒度才能逐段打分。

策略：
1. baseline 输出按 `\n\n+` 拆分得到 baseline 段落列表 `B[]`
2. ABI 原文段落列表 `S[]`
3. 若 `len(B) == len(S)`：直接位置对齐（`S[i] ↔ B[i] ↔ A[i]`）
4. 若 `|len(B) - len(S)| ≤ 0.1 * len(S)`：用 [Needleman-Wunsch on lengths] 做一次软对齐；不匹配的样本标记 `align_failed` 并排除（计入 baseline 的 `completeness` 失败）
5. 若差距更大：宣告 baseline 重组段落严重，整轮 baseline 标记降级 `baseline_align_failed`，仅给出文档级 judge 评分

## 流水线

```
abi eval <source>
    [--abi-run <run_id>|latest]   # 默认使用最近一次 run；要求 source 与 run 的 book_id 一致
    [--samples 30]
    [--judge-model <name>]        # 默认与 LLM_MODEL 相同
    [--baseline-chunk-tokens 50000]
    [--skip-baseline]              # 复用已有 baseline 翻译
    [-o eval-out/]
```

执行顺序：

1. 加载 ABI run（`runs/<book_id>/<run_id>/`）→ `book`、`units`、`glossary`、`headings`
2. 生成 baseline（若 `--skip-baseline` 且文件存在则复用）
3. 对齐 baseline 段落 ↔ ABI 段落 ↔ source 段落
4. 抽样
5. 计算机械指标（ABI 全集 + baseline 全集 + 抽样集）
6. LLM judge：每个样本一次 likert（A+B 同时打分）+ 一次 pairwise
7. 聚合并渲染报告

## 产物布局

```
eval-out/<book_id>/<eval_id>/
├── manifest.json
├── baseline/
│   ├── translated.md          # baseline 原始拼接输出
│   └── meta.json              # 模型、tokens、cost、latency、chunk 数
├── alignment.json             # S/A/B 对齐结果与对齐失败列表
├── samples.jsonl              # 每行一个抽样三元组
├── mechanical.json            # 机械指标聚合
├── judge/
│   ├── likert.jsonl
│   └── pairwise.jsonl
├── events.jsonl               # 与 translate run 一致的事件流
├── report.json                # 机器可读总览
└── report.md                  # 人类可读总览
```

## Observability

复用 `EventLogger` / `MetricsAggregator` / `LLMRouter`。所有 LLM 调用（baseline_translator、eval_judge_*）走 router，自动获得：
- Langfuse trace
- 重试 + 指数退避
- 成本预算 + 硬上限
- agent.call 事件

Eval pipeline 自身的事件用 `eval.*` 前缀（`eval.start`、`eval.baseline.start`、`eval.sample`、`eval.judge.likert`、`eval.judge.pairwise`、`eval.aggregate`、`eval.end`）。

## 安全 & 限速

- Judge 调用与 baseline 翻译共用同一个 `LLMRouter`，受 `cost.hard_cap_usd` 约束。
- 抽样默认上限 30 + 全书 baseline 拼接，预算超限时 graceful stop（已完成的样本仍写入 `samples.jsonl` 与 `judge/*.jsonl`）。
- judge prompt 不会把"哪一侧是 ABI"信息透露给模型；A/B 标签随机化，对应关系记录在 `alignment.json` / `samples.jsonl` 内部，不出现在 prompt 中。

## 不在 0.1 范围内（后续）

- BLEU/COMET/BERTScore（需要参考译本，academic books 很少有）
- 多 judge 投票（cost × 3）
- Inter-annotator agreement（需要人工 + judge 联合）
- 翻译错误类型学（MQM）
