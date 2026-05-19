# QUALITY_SCORE.md

> 翻译质量的**机械可测**评分体系。不是为了"看着好看"，而是为了：
> 1. 让回归可监控（PR 不允许显著降低分数）
> 2. 让 flagged 段落被精准定位
> 3. 让"质量提升"成为可优化的指标

## 评分粒度

- **段落级**（`TranslationUnit.confidence`）：0..1
- **章节级**：段落聚合 + 章节级一致性检查
- **全书级**：章节聚合 + 全书一致性 + 成本/速度

## 段落级分数

### 子项与权重

```
score = (
    0.30 * schema_valid
  + 0.25 * term_compliance
  + 0.15 * length_ratio_ok
  + 0.10 * anchor_preserved
  + 0.10 * llm_self_confidence
  + 0.10 * no_refusal_no_residue
)
```

每子项归一到 [0, 1]：

#### `schema_valid` (0/1)
LLM 输出能否被 pydantic 解析。失败为 0。

#### `term_compliance` (0..1)
```
= 1 - (违反 locked term 数 / source 中 locked term 总数)
```
分母为 0 时取 1。

#### `length_ratio_ok` (0..1)
预期 ratio 区间按语言对（见下表），用平滑函数：
```
ratio = len(translated) / len(source)
if lo <= ratio <= hi: 1.0
elif lo*0.7 <= ratio < lo or hi < ratio <= hi*1.3:
    线性衰减到 0.5
else:
    0.2
```

| 源→目标 | lo | hi |
| --- | --- | --- |
| en→zh | 0.22 | 0.55 |
| zh→en | 1.5 | 4.0 |
| ja→zh | 0.45 | 1.0 |
| en→ja | 0.6 | 1.4 |
| zh→ja | 0.9 | 1.6 |

> v0.1 阈值依据：用 DeepSeek-V4-Pro 翻译 5 段英文学术原文，en→zh 字符比集中在 0.27-0.33；汉字信息密度高，比例显著低于早期"经验起点 0.45"。v0.2 将基于更大样本集（≥ 500 段）正式校准。

#### `anchor_preserved` (0..1)
```
= 出现在译文中的 anchor 数 / source 中 anchor 数
```

#### `llm_self_confidence` (0..1)
LLM 自评。仅作弱信号（已知不可信），权重低。

#### `no_refusal_no_residue` (0/1)
- 不含 "I cannot" / "无法翻译" / "as an AI" 等模式 → 1
- 不含大量源语言字符残留（按 unicode block 启发） → 1
- 任一失败 → 0

### Flag 触发阈值

| flag | 触发条件 |
| --- | --- |
| `term_drift` | term_compliance < 1.0 |
| `length_ratio_outlier` | length_ratio_ok < 0.5 |
| `schema_error` | schema_valid == 0 |
| `refusal_detected` | no_refusal_no_residue == 0 |
| `low_confidence` | 总分 < 0.7 |
| `untranslated_residue` | 残留检测命中 |
| `passthrough` | 合法跳过翻译：code block / equation / 空段。`translated_text == source_text`，confidence=1.0。 |
| `translation_failed` | 翻译器硬失败（异常或 JSON 解析失败）。`translated_text == ""`（绝不回填源文）。永远与 `schema_error` 共存；通过缺少 `passthrough` 与合法跳过区分。 |

## 章节级聚合

```python
class ChapterQualityReport:
    section_id: str
    n_paragraphs: int
    score_avg: float
    score_p10: float        # 10 分位
    flagged: dict[Flag, int]
    term_consistency: float # 章节内同一 term 不同译法的比例反义
    style_drift: float | None  # 用 embedding 比对前 5 章 vs 当前章的语气向量
```

`term_consistency`：章节内出现 N 次的 term，应每次都翻译成 `glossary.target`。
```
= 1 - (违反次数 / 总出现次数)
```

## 全书级报告

```python
class BookQualityReport:
    book_id: str
    run_id: str
    score_overall: float            # 段落分数加权平均（权重 = paragraph 字数）
    score_by_chapter: dict
    flagged_counts: dict[Flag, int]
    flagged_paragraphs: list[str]   # paragraph_id
    term_compliance_overall: float
    length_ratio_distribution: histogram
    cost_usd: float
    tokens: TokenUsage
    duration_s: float
```

## 黄金集 & 回归

`tests/quality/golden/` 包含若干"金标段落"：
- 经人工校对、定为"正确翻译"
- 每次 PR CI 跑这些段落
- 如果新版本（prompt / 模型 / 参数）使**任何金标段落分数下降 > 0.05**，PR 阻断

这是 OpenAI Codex 工程文章中"用机械规则保护品味"的具体落地。

## 可选：自动评审

`--quality high` 时启用 `TranslationReviewer`：
- 另一个 LLM 调用（可用更便宜模型），仅做评分
- 输出 `(score, strengths, weaknesses)`
- 与本地启发式分数加权（默认 0.5/0.5）

注意：评审本身可能出错，**不**作为唯一信号。

## 报告呈现

`report.md` 中包含：
- 总分 + 分布直方图
- 每章评分表
- Flag 计数与 Top-10 段落清单（带 paragraph_id + 摘录）
- 与上一次同书 run 的对比（如有）

## 不变量

1. 段落分数计算是**纯函数**，无 LLM 调用（除可选 reviewer）
2. 分数公式变更必须：递增版本号 + 重跑所有现存 run 的报告 + 在 CHANGELOG 中说明
3. flag 触发条件与上表一致；不允许"特殊段不算"的硬编码豁免（必要时通过 `attrs.skip_quality_check` + 显式记录）
