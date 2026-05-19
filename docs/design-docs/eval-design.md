# Evaluation pipeline — design

## 目标

衡量 ABI 多 pass 翻译相对于"原文整段塞进上下文一次性翻译"baseline 的质量差异，给出可比较、可解释、可复现的多维评测报告。

非目标：取代人工质检；给出绝对质量分（仅给出 ABI vs baseline 的相对差）。

## 评测对象

支持 **2-way** 与 **3-way** 两种比较：

- **A 系统（ABI）**：本仓库 `abi translate` 的产物（per-paragraph `TranslationUnit`，含 confidence、flags、glossary 合规等）。
- **B 系统（Baseline）**：把全书源文直接塞进单条 prompt，要求模型一次性输出译文。当书超出上下文窗口时，按固定大块（默认 50K input tokens / 块）顺序翻译并拼接。除分块外不做任何 ABI 风格的预处理。
- **C 系统（Human Reference）**：仅当输入来自带人工参考译文的数据集（当前支持 [`google/wmt24pp`](https://huggingface.co/datasets/google/wmt24pp) literary 子集与 [`Helsinki-NLP/news_commentary`](https://huggingface.co/datasets/Helsinki-NLP/news_commentary)）时启用。
- **共同输入**：同一份源文（来自本地文件 _或_ 数据集 spec）、同一目标语言、同一翻译模型（`LLM_MODEL`）。
- **Judge 模型**：默认与翻译模型一致；可通过 `EVAL_JUDGE_MODEL` 环境变量或 `--judge-model` 切换为更强模型。**Judge 与 agent 共用同一 `LLM_BASE_URL` 与 `LLM_API_KEY`**，只换模型名。

## 输入源

| 模式 | 触发方式 | 备注 |
| --- | --- | --- |
| 本地文件 | `abi eval <path>` | 与 `abi translate` 相同的 ingester（txt / epub） |
| 数据集 | `abi eval --dataset <spec>` | spec 形如 `wmt24pp:en-zh_CN:literary` 或加 `:stub=true`（本地测试） |

数据集模式下，adapter 将 dataset 物化为一个临时 `.txt` 文件（每 `document_id` 一章）走标准 ingest，从而保留 1:1 段落对齐。`--auto-translate` 会在缺少 ABI run 时自动跑一次 `abi translate`。

### 已接入数据集

| Spec | 来源 | 语种 | 主要用途 | 段落规模 | 文档边界 |
| --- | --- | --- | --- | --- | --- |
| `wmt24pp:en-zh_CN:literary` | `google/wmt24pp` | en → zh_CN | 文学类（literary register）judge 校准 | ~数千 (literary subset) | 数据集自带 `document_id` |
| `news_commentary:en-zh:academic-accessible` | `Helsinki-NLP/news_commentary` | en → zh | 学术可读类（academic-accessible register） — 经济、政治、政策类专家评论 | ~69k 行 | **启发式重建**：长度 < 90 字符且下一行 ≥ 150 字符的行视为标题，作为新文档起点 |

`news_commentary` 没有原始 document boundary，启发式在 200 行随机样本上无误报；少量漏报会把两篇文章合并，对 judge 的 `coherence` 维度只产生轻微干扰。可通过 `:title_max_chars=N:body_min_chars=M` 调整阈值。

通用选项：`limit_docs=N`（仅取前 N 个文档）、`stub=true`（adapter 内置的离线 fixture，供 CI 使用）。

## 维度

### 机械指标（确定性，零额外 LLM 调用）

| 维度 | 说明 | 计算方式 |
| --- | --- | --- |
| `glossary_compliance` | 译文是否使用 ABI 锁定术语的官方目标语 | 扫描每个样本译文，统计核心术语命中 / 命中正确 |
| `length_ratio` | 译文 / 源文字符比是否落在合理区间 | 与 `translate/validator.py` 相同区间（en→zh: 0.22-0.55） |
| `anchor_preservation` | 数字、年份、专名、URL、引号内字符串保留率 | 抽取源文中的正则 anchors → 检查译文是否完整出现 |
| `completeness` | 段落级别是否漏译 | baseline 段落与源段落对齐失败 / 译文为空 / passthrough |

### LLM-as-Judge（每样本一次 likert + 一次 pairwise 调用）

| 维度 | 提示 | 输出 |
| --- | --- | --- |
| `adequacy` | "Does the translation convey all key information of the source?" | Likert 1-5 |
| `fluency` | "Is the translation natural and grammatical in the target language?" | Likert 1-5 |
| `coherence` | "Does the translation read coherently in context (prev/next 2 paragraphs given)?" | Likert 1-5 |
| `style` | "Does the translation match the expected register (academic-formal here)?" | Likert 1-5 |
| `pairwise` | 给出全部参与系统的译文（**随机化 A/B/C 槽位消除位置偏置**），要求 judge 选偏好侧并给理由 | 2-way: `prefer ∈ {A, B, tie}`；3-way: 三组 `{a_vs_b, a_vs_c, b_vs_c}` |

Judge 在同一个 prompt 中同时给全部系统打 Likert 分数（2-way 给 A/B；3-way 给 A/B/C），避免分别打分时的"绝对值漂移"。Pairwise 单独一次调用：2-way A/B 顺序随机翻转，3-way A/B/C 槽位随机置换。

Prompt 模板：

- 2-way: `eval_judge_likert/v1` + `eval_judge_pairwise/v1`
- 3-way: `eval_judge_likert_3way/v1` + `eval_judge_pairwise_3way/v1`

`triple.has_reference` 决定走哪条路径。

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
abi eval [<source>]
    [--dataset <spec>]            # e.g. wmt24pp:en-zh_CN:literary[:stub=true]
    [--limit-docs N]              # only first N documents of the dataset
    [--auto-translate]            # run `abi translate` first if no ABI run exists
    [--abi-run <run_id>|latest]   # 默认使用最近一次 run
    [--samples 30]
    [--judge-model <name>]        # CLI > EVAL_JUDGE_MODEL env > LLM_MODEL
    [--baseline-chunk-tokens 50000]
    [--skip-baseline]              # 复用已有 baseline 翻译
    [--no-langfuse-experiment]     # 关闭 Langfuse experiment（默认开）
    [-o eval-out/]
```

执行顺序：

1. 解析输入：本地文件 _或_ dataset → 物化为 `.txt`（必要时 `auto_translate` 跑一次 `abi translate`）
2. 加载 ABI run（`runs/<book_id>/<run_id>/`）→ `book`、`units`、`glossary`、`headings`
3. 生成 baseline（若 `--skip-baseline` 且文件存在则复用）
4. 对齐 source ↔ ABI ↔ baseline（+ reference if any）
5. 抽样（按章节分层 + 首末段强制纳入）
6. 计算机械指标（ABI 全集 + baseline 全集 + reference 抽样集 + 抽样 ABI/baseline）
7. LLM judge：每个样本一次 likert（同时给 A/B 或 A/B/C 打分）+ 一次 pairwise
8. 聚合并渲染报告；可选写入 Langfuse Experiment

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

### Langfuse Experiments

数据集模式下，pipeline 自动维护一个 Langfuse Dataset：

| Langfuse 概念 | 对应 ABI 概念 |
| --- | --- |
| Dataset name | `<adapter>-<lang_pair>-<register>-v1`（可被 `--langfuse-dataset` 覆盖） |
| Dataset item | 每个数据集段落（`paragraph_id` 为稳定哈希，多次 push 是幂等的） |
| Dataset run id | `eval_id`（每次 `abi eval` 一个新 run） |
| Trace | 每个抽样段落一个 trace（包含该段所有 LLM 调用） |
| Score | 机械 + likert + pairwise + 聚合 |

无 Langfuse 密钥时自动跳过；`--no-langfuse-experiment` 强制关闭。

## 安全 & 限速

- Judge 调用与 baseline 翻译共用同一个 `LLMRouter`，受 `cost.hard_cap_usd` 约束。
- 抽样默认上限 30 + 全书 baseline 拼接，预算超限时 graceful stop（已完成的样本仍写入 `samples.jsonl` 与 `judge/*.jsonl`）。
- judge prompt 不会把"哪一侧是 ABI"信息透露给模型；A/B 标签随机化，对应关系记录在 `alignment.json` / `samples.jsonl` 内部，不出现在 prompt 中。

## 不在当前范围（后续）

- 自动 BLEU/COMET/BERTScore（有 reference 时可以加，目前先靠 LLM judge）
- 多 judge 投票（cost × N）
- Inter-annotator agreement（需要人工 + judge 联合）
- 翻译错误类型学（MQM）
- 学术书完整冷启动数据集（目前仅有 WMT24++ literary 与 News-Commentary，长篇 academic books 仍需人工收集）
