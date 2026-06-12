# 质量门禁框架 / Quality Gate Framework

> 原始 AI 草稿不可发布。每个门禁未通过前不得进入下一阶段。
> Raw AI output is not publishable. No stage may advance until its gate passes.

## 门禁序列 / Gate sequence

1. **版权证据** — 翻译前 `metadata/rights_checklist.md` 必须明确 PASS。
2. **研究 + 文体画像** — 批量翻译前 `metadata/book_specific_translation_research.md` 与
   `metadata/style_profile.md` 必须存在。
3. **试译 PASS** — `qa/pretranslation/pretranslation_report.md` 结论必须为 `PASS`。
4. **每章译后全量控制 (08a)** — 进入下一章前的硬门禁。最近一轮必须是零问题 PASS：
   `scope: FULL_CHAPTER`, `issues_found: 0`, `fixes_applied: 0`,
   `unresolved_blocking_issues: 0`, `latest_round_status: PASS`, `allow_next_chapter: true`。
5. **忠实度 / 可读性+意象 / 术语** 三项审校。
6. **章节门禁 (11)** — `qa/gates/{NNN_slug}.gate.md` PASS 后，章节才能进入 `chapters/final/`。
7. **预制作规格 + 样章 EPUB PASS** — 全书构建前必须完成。
8. **出版文本 lint + 资源引用检查** — 构建 EPUB 前必须无硬错误。
9. **分层随机抽检** — 第一版 EPUB 后，至少两个独立 Agent；优秀线 avg>=92, min>=88；
   `release_confidence >= 0.80`。
10. **独立双 Agent 评审**。
11. **版本化发布** — `release_state.json.latest_status == PASS`。

## 专家级翻译与一词多义 / Expert translation & polysemy (mandatory)

专家级翻译质量是工作流要求，不是可选润色。章节控制 PASS 必须包含机器可读字段：

```
expert_translation_skill_used: true
expert_level_review_status: "PASS"
polysemy_translation_stage_review: "PASS"
polysemy_context_review: "PASS"
polysemy_unresolved_count: 0
```

## 硬失败线 vs 优秀线 / Hard floor vs excellence line

- `80` 只是硬失败线。任一单项 `< 80`，或任一 P0/P1/P2，即使平均分达标也判失败。
- `80-87` 仍需精修；`88-91` 较好但未达最终优秀门槛；最终退出要求每个 Agent
  `average_score >= 92` 且 `lowest_score >= 88`。

## 问题族全书审计 / Book-wide defect-family audit

任一抽检发现需要修复或可能系统性复现的问题，必须先归纳为问题族，再对整本读者可见
书稿执行同类问题审计（不只修被抽中样本），并在 `fix_log.md` 与 `closure_check.md`
关闭后才能用新 seed 复抽。可复用经验回填到
`skills/translation-quality-defect-families/SKILL.md`。
