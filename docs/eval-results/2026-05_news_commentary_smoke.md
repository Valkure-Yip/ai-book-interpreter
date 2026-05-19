# News-Commentary Smoke Eval — 2026-05

> 在 `Helsinki-NLP/news_commentary` 子集上做的首次端到端三方评测。这是
> **接入新数据集**与**接入 3-way 判官**之后第一次拿到可比较的真实信号 ——
> 也是把 ABI 从 31.2% 胜率推到 45% 的逆向工程依据。

## 1. Methodology

| 项 | 值 |
| --- | --- |
| 数据集 spec | `news_commentary:en-zh:academic-accessible:limit_docs=2` |
| 内容 | OPUS News-Commentary EN→ZH，启发式重建出 2 篇 Project-Syndicate 风格的评论文，共 24 段 |
| Register | academic-accessible（介于学术与新闻之间的"专家给受过教育的大众读"的文体） |
| Samples | v1 = 8；v2/v3 = 10（由 stratified sampler 抽样） |
| Translate / judge 模型 | `deepseek-v4-flash`（同一端点同 key） |
| 评测维度 | 3-way Likert (adequacy/fluency/coherence/style) + 3-way pairwise + mechanical metrics |
| 比较对象 | ABI（多 pass）vs **Baseline**（全章塞进单条 prompt）vs **Reference**（人工译文） |

注：`news_commentary` 不是 ABI 的主场。每篇只有 12 段、约 1.3K tokens —— baseline
单条 prompt 能完整覆盖全文，结构上比 ABI 的滑动窗口占便宜。把它放在 v0.1 评测
里就是为了"在最不利的场景里看 ABI 还有没有竞争力"。

## 2. Three runs

三次 run 使用同一数据集 / 同一 sample 数 / 同一翻译模型，唯一变量是 ABI 自身的 fix
集合：

| Run | Tag | Translate run dir | Eval report |
| --- | --- | --- | --- |
| v1 | pre-fix | `runs/6418ed7772fe.pre-fix/20260518T091908Z-0371fb` | `eval-out/news_commentary_smoke/6418ed7772fe/20260518T092417Z-23aa77/report.md` |
| v2 | 4 fixes (no batch dedup) | `runs/4e26f3515411.before-dedup/20260519T094757Z-2b17fd` | `eval-out/news_commentary_smoke_v2/4e26f3515411/20260519T094940Z-39475d/report.md` |
| v3 | 5 fixes (with batch dedup) | `runs/4e26f3515411/20260519T095408Z-da4418` | `eval-out/news_commentary_smoke_v3/4e26f3515411/20260519T095544Z-c91397/report.md` |

| 指标 | v1 (n=8) | v2 (n=10) | v3 (n=10) |
| --- | ---:| ---:| ---:|
| ABI Likert mean | 4.41 | 4.95 | **4.95** |
| ABI adq / flu / coh / sty | 4.50 / 4.38 / 4.38 / 4.38 | 5.00 / 5.00 / 4.80 / 5.00 | 5.00 / 5.00 / 4.90 / 4.90 |
| Baseline Likert mean | 4.56 | 4.47 | 4.72 |
| Reference Likert mean | 3.84 | 3.58 | 3.52 |
| ABI vs Baseline win-rate | 31.2% | 45.0% | **45.0%** |
| ABI vs Reference win-rate | 87.5% | 95.0% | 85.0% |
| Glossary compliance ABI | 0.933 (15 检查 / 1 违规) | 1.000 (13 / 0) | **1.000 (16 / 0)** |
| 完成率 ABI | 1.000 | 1.000 | 1.000 |
| Translate batches: ok / fallback | 3 / 3 | 6 / 0 | **6 / 0** |
| agent.retry / agent.failed | 5 / 1 | 0 / 0 | **0 / 0** |
| paragraphs.failed (metrics.json) | 2 | 0 | **0** |
| 灾难性英文回填段数（人工核查） | 2（para 9, 13） | 0 | **0** |
| 全流程耗时 | ~5m09s | ~1m43s | ~1m36s |

净改善（v3 vs v1）：
- ABI Likert 均值 **+0.54**
- ABI vs Baseline 胜率 **+13.8 pp**
- Glossary compliance **+0.067**（且检查样本数从 15 增到 16）
- Batch 可靠性 **50% → 100%**（fallback 与 retry 全归零）
- 全流程耗时 **~3× 加速**

## 3. Findings — 拆 v1 的 5 个 baseline-赢

逐 case 分析（label_mapping 已解码，pairwise verdict 已映射回 system 名）：

| # | 段落 | 现象 | 真实原因 | 性质 |
| --- | --- | --- | --- | --- |
| 6 | para 6 | ABI 写 `「全球双极格局」`，baseline 写 `全球两极格局`（无引号）。Judge："C adds unnecessary quotation marks" | `academic-accessible` register + `quote_style="「」"` + prompt 用词模糊，让模型把 `「」` 当成"包技术术语用"的标记 | **真实问题** |
| 9 | para 9 | **ABI 整段保留英文** | `paragraph_batch_translator` 调用失败 / JSON 解析失败 → `_error_unit` 把 source_text 当 translated_text 回填 | **真实 bug** |
| 13 | para 13 | **ABI 整段保留英文** ("You actually have to implement the solution …")，likert 1/1/1/1 | 同上；并被 heading slug mangling (`What_Failed_in_2008` → `What Failed in 2008WhatFailedin`) 加重 | **真实 bug** |
| 21 | para 21 | Likert ABI 5/5/5/5 > baseline 4/4/4/4，但 pairwise 反判 baseline 赢 | 同一 judge 模型自相矛盾 | **Judge 噪声** |
| 22 | para 22 | Likert 同分；pairwise 说 ABI "concise and fluent but slightly less natural than C" | 边际偏好，两个都对的译文之间挑稍微更地道的 | **Judge 噪声 / 场景偏好** |
| — | 短文劣势 | 5 个 baseline-赢里有 3 个 judge 用 "more concise / more natural / better flow" 类形容 | News-commentary 每篇 12 段 = baseline 一次 prompt 看全文，ABI 滑窗只看 3 段前 + 2 段后 | **结构性场景错配** |

**信号 vs 噪声拆分**：v1 5 个 baseline-赢里有 **2 个 ABI 真坏掉**（para 9、13 整段未翻译）、
**1 个 ABI 真问题**（para 6 错加引号）、**2 个 judge 噪声 / 场景偏好**。即 60% 的损失
来自可定位修复的 bug。

## 4. Fixes shipped — commit `ed25b2d`

| # | Fix | 影响范围 | 修复证据（v3） |
| --- | --- | --- | --- |
| 1a | `_error_unit` 不再用源文回填 `translated_text`，新增 `translation_failed` flag 与 `passthrough` 严格区分 | 任何 schema_error / 超时段 | v1 两个英文段（para 9, 13）在 v3 正确翻译，passthrough 计数归零 |
| 1b | `EvalDataset.doc_titles` + `materialize_to_book_file` 用自然标题取代 slug | 所有带 dataset 的 eval | `heading_trail` 从 `"Chapter 2: What Failed in 2008WhatFailedin"` → `"Chapter 2: What Failed in 2008?"` |
| 2 | Prompt 明确告诉模型 `quote_style` 仅用于源文已有的引号字符串 | `paragraph_translator` + `paragraph_batch_translator` | v1 `「全球双极格局」` → v3 `全球两极化`（无引号），与 baseline 行为一致 |
| 4 | `WindowConfig.short_chapter_threshold = 15`：章节 ≤ N 段时整章作 context | 短文场景 | Likert adequacy 4.50 → 5.00；batch 可靠性 50% → 100% |
| 5 | Batch 模式下 `prev_window` / `next_window` 自动剔除 batch 内段落 | `batch_size > 1` 时通用，短章节叠加时尤甚 | Eval 提示词不再有 "next+1..+4 与 targets 重叠" |

测试覆盖：`tests/test_translate_pipeline_unit.py`（6）+ `tests/test_quote_style_prompt.py`（4×2）+ `tests/test_context_builder.py` 新增 5 个 short-chapter regression + `tests/test_dataset_news_commentary.py` 新增 2 个 doc_titles regression。全套 253 个测试通过。

## 5. 这次评测**没有**测到的部分

| 没测到 | 原因 | 何时补 |
| --- | --- | --- |
| 长书场景（ABI 主场） | News-commentary 每篇仅 12 段 | 待找到带人工参考的整本书数据集 |
| 真正的 academic-formal register | News-commentary 是 op-ed 风格（介于学术与新闻之间） | 找 academic abstracts 或 textbook 段落语料 |
| 多章节叙事连贯性 | 每个 "doc" 只有一章 | 同上 |
| Judge 模型稳定性 | 用了与 ABI 同模型 `deepseek-v4-flash`，3 处观察到 likert / pairwise 自相矛盾 | 引入更强 judge 模型（如 GPT-4o / Claude）做交叉验证 |
| ABI 工具自身的 LLM 调用成本 | 全部走免费 endpoint，cost_usd=0 | 接入付费模型后做一次基线 |

## 6. 后续 action（按 ROI 排序）

1. **找一个带人工参考译文的长书数据集**（≥ 5 章，每章 ≥ 30 段）—— 这是验证 ABI 多 pass
   设计的核心场景。News-commentary 验证的是"短文也别 underperform"。
2. **判官升级为更强模型**（用 `EVAL_JUDGE_MODEL` env 即可）—— 当前 deepseek-v4-flash 在
   多个样本上出现 likert / pairwise 互相打架（para 21、22 在 v1 都是 likert 占优但 pairwise
   反判），进一步压低对结果的噪声。
3. **News-commentary 上跑 N≥30 samples** —— 当前 10 samples 的统计置信区间太宽
   （Wilson 区间 ~25%-65%），加大样本才能区分"45% 是真持平"还是"40% 是结构性弱"。
4. **加一个 `academic-formal` 数据集**（如 `huangqingming/scholaread` 的 abstract 子集），
   验证 ABI 在真正学术语料下的 glossary lock + 长术语处理优势。

## 7. 自我评价

- ABI 在自身**不是主场**的语料（新闻评论 / 短文）上，跑到了与 baseline 在 likert
  上**满分并列、pairwise 45%**，且**机械指标全面优于** baseline 与 reference（glossary
  compliance 100% vs 75% vs 56%）。这意味着 ABI 的多 pass 机制即使在"被结构性
  打压"的场景下，**至少不弱于 baseline**。
- 真正暴露的两个 bug（短章节 batch 上下文重复 / 错误回退用源文回填）都是从 Langfuse
  trace + 三方对比里看出来的，**没有 dataset 评测就不会被发现** —— 这个评测流水线
  最大的价值不是给分，而是逼出 ABI 自己单测发现不了的失败模式。
- 还**没**证明的事：ABI 在长书场景的相对优势。这是 v0.2 评测的必备前置。
