---
name: translation-quality-defect-families
description: Registry of recurring translation-quality defect families with detect/fix/recheck patterns. Use when translating, reviewing, fixing, or retrospectively summarizing recurring problems.
---

# Translation Quality Defect Families

> 发现可复现质量问题时：归纳 → 记录即时证据 → 低 token 全书同类审计 → 修复 → 回填本技能 → 复查。
> On any recurring issue: classify, record evidence, low-token book-wide audit, fix, backfill this skill, recheck.

## 已登记问题族 / Registered families

- **gate-passing style debt** — 80-91 分被当成优秀；通顺但无味的第一版当终稿。
- **short-sentence fragmentation** — 把中文压成动作清单/短句切断，丢失语流。
- **metaphor collision** — 相邻比喻物自撞；为生动添加原文没有的比喻、声音、情节。
- **source-syntax residue** — 源语句法残留、过硬过直、学术腔、读者难懂。
- **terminology drift** — 同一术语跨章漂移；正文出现禁用写法或裸露源语词。
- **over-explanation / added drama** — 加戏、过度解释，超出原文信息。
- **title-chain literalism** — 旧纸书 `--` 长标题机械转成中文破折号长链。

## 工作流 / Workflow

1. 分类（属于哪个族；新族则新增条目）。
2. 在问题轮次 `fix_log.md` 记录即时证据。
3. 低 token 审计同类：优先 `rg`、术语表、禁用正文写法表、标题映射、抽样 manifest、
   章节控制记录、小上下文原文对照；只有候选片段进入 agent 复核。
4. 修复所有确认命中，记录合理例外。
5. 回填本技能：在 `fix_log.md` 填写
   `translation_quality_skill_backfill: "UPDATED" | "MERGED" | "NOT_APPLICABLE"`、
   `translation_quality_skill_backfill_path`、`translation_quality_skill_backfill_summary`；
   在 `closure_check.md` 填写 `translation_quality_skill_backfill_verified: true`。
6. 复查闭环：`open_p0_p1_p2_count = 0`。
