# QUALITY_SCORE.md

> ABI 的质量结论来自分层 evidence 与确定性 validator，不来自单一“总分”或模型自评。
> 流程可信度、逐章译文和最终 EPUB 任一平面失败，整本书都不能发布。

## 1. 三个评测平面

| 平面 | 对象 | PASS 的含义 |
| --- | --- | --- |
| L1 | plan、Action、attempt、receipt、gate、recovery lineage | 权限、门禁与提交不可绕过，durable facts 可重放 |
| L2 | 研究、术语、`translated/controlled/final` 章节与 QA | 译文完整、忠实、可读、术语和格式符合要求 |
| L3 | EPUB、独立评审、随机抽检与 release | 成品可解析、制作合规，且满足最终卓越线 |

详细 eval 协议见 [`design-docs/eval-standard.md`](./design-docs/eval-standard.md)。本文件聚焦 L2/L3 的
质量门禁。

## 2. 章节质量链

每章必须经过不可变的三层工件：

```text
chapters/src/{chapter}.md
  → chapters/translated/{chapter}.md
  → chapters/controlled/{chapter}.md
  → chapters/final/{chapter}.md
```

### 2.1 初译

初译 Action 只接收原章、5–8 条关键风格规则和命中术语，只输出非空译文。格式/schema 错误、漏写或
工具失败不会用原文回填；它们产生 typed failure 或 incomplete-output retry evidence。

### 2.2 章控

`chapter.control` 必须同时产出 substantive controlled revision 与当前最后一轮的连续八字段协议块：

```text
scope: FULL_CHAPTER
issues_found: 0
fixes_applied: 0
unresolved_blocking_issues: 0
latest_round_status: PASS
allow_next_chapter: true
expert_translation_skill_used: true
polysemy_unresolved_count: 0
```

旧 PASS、重复字段、空行拼接或后置块都不能掩盖最新轮 FAIL。

### 2.3 章节评审

`chapter.review` 对每章使用隔离 agent loop，输出：

- 忠实度报告；
- 可读性报告；
- 意象/语气报告；
- 术语报告；
- 章节 gate；
- `chapters/final/{chapter}.md`。

validator 要求全部 expected outputs 非空，且 gate 明确 `result: PASS`。失败 excerpt 保留
`chapters/final/...` 路径，使 repair 能精确定位受影响章节。

## 3. 确定性检查

LLM 评审不能覆盖以下硬失败：

- expected manifest 缺失、额外输出、空文件或 checksum 不匹配；
- 原文章节与译文章节集合不一致；
- locked/preferred 术语、禁用 rendering 或明显未译残留违规；
- 截断、重复、结构/anchor 丢失；
- 中文最终章节中与 CJK 相邻的 ASCII 引号等出版格式错误；
- EPUB 资源、导航、媒体类型、ZIP 或 OPF 不一致；
- publication lint、asset manifest check 或 EPUBCheck 非 PASS。

每个 validator 决定都绑定 identity/version、bundle digest 与 ordered checksums；只有 gate receipt 和完整
promotion intents 一起持久化后，工件才允许提升到 canonical。

## 4. 独立双审

最终 EPUB 前必须运行两个职责分离的 reviewer：

- **agent A：** 译文质量，关注忠实、完整、可读、术语与章节级问题；
- **agent B：** 成品与排版，区分章节 typography 问题和 EPUB 制作问题。

`reviews/agent_a/review.md`、`reviews/agent_b/review.md` 必须各自以唯一的
`result: PASS|FAIL` 终止，`reviews/revision_route.md` 在两者 PASS 时也必须终止为 PASS。FAIL 根据证据分为：

| reason code | repair 方向 |
| --- | --- |
| `translation_quality_failed` | 修订受影响的最终章节 |
| `chapter_typography_failed` | 修复章节文本/标点，再重建后继工件 |
| `epub_quality_failed` | 修复制作规格或 EPUB 构建 |
| `independent_review_protocol_invalid` | 修复评审协议输出，不把格式错误当作质量结论 |

## 5. 分层随机抽检

每轮使用存储的 seed、章节集合与 strata 为至少两个 reviewer 独立采样。reviewer summary 的硬阈值为：

| 指标 | 阈值 |
| --- | --- |
| 每位 reviewer 平均分 | `>= 92` |
| 每位 reviewer 最低分 | `>= 88` |
| 任一 sample | `>= 80` |
| open P0/P1/P2 | `0` |
| release confidence（所有 reviewer 最小值） | `>= 0.80` |
| 连续 PASS 轮数 | 默认 `2` |

连续轮数由 `ABI_SPOTCHECK_PASS_ROUNDS` 配置，最小为 1。生产默认 2；降低为 1 只能视为明确的 smoke/
验证配置。validator 会从 reviewer-owned summaries 重算 `validation_report.json`，不会相信 agent 自写的
最终 verdict；summary 分数必须与 sample verdict 一致。

## 6. EPUB 与发布门禁

`epub.build` 要求以下工件全部存在且 JSON 报告 `ok=true`：

- `output/book.epub`；
- `output/publication_lint.json`；
- `output/asset_manifest_check.json`；
- `output/epubcheck.json`。

`release.prepare` 还要求 current independent review 与 spot-check 成功。发布状态必须包含唯一匹配版本、
`latest_status=PASS`、正确 EPUB 文件名与实际版本化副本。run completion 还要求 final manifest、复盘工件
和没有 blocking incident。

## 7. 缺陷与收敛

质量 FAIL 不直接变成重试。validator 产生 typed reason，Registry 映射到具体 repair capability；controller
创建新 plan/action/staging，保留原失败证据。重复修复由 `ABI_MAX_SEMANTIC_REPAIR_ATTEMPTS` 限制；达到
上限进入 `semantic_repair_stalled`，避免无限自循环。

integrity、未知分类或无法证明的执行结果不是质量 repair，必须 `BLOCKED` 并等待人工证据。

## 8. 回归要求

- 阈值、抽样 strata 或 validator identity/version 变更必须带测试与设计记录；
- L2/L3 golden fixture 不能因宏观编排重构而静默改变判据；
- `abi eval trace PROJECT_ROOT` 检查 L1；
- `abi eval book PROJECT_ROOT` 汇总 L1/L2/L3；
- 模型 judge 只提供语义证据，不能覆盖确定性失败。
