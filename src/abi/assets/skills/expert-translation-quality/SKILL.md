---
name: expert-translation-quality
description: Expert-level prose, context-dependent word choice, and polysemy back-checking for translation, review, revision, and spot-check stages.
---

# Expert Translation Quality

> 这是工作流节点上加载的程序性技能，不要把整份内容塞进每个翻译 prompt。
> A procedural skill loaded at workflow nodes. Do not paste it into every translation call.

## 1. 翻译前定向 / Orient before translating

在翻译一章前，先建立内部观察清单：一词多义词、习语、文化负载词、随上下文变义的术语、
作者反复使用的母题词。记下哪些只能靠后文判定。

## 2. 翻译阶段承担一词多义 / Translation stage owns polysemy

- 能在本地（句/段）判定的词义，当场定稿。
- 不能本地判定的，标记为待回查（unresolved），进入专家复查。
- 不得把歧义留给读者或下游 QA 默认正确。

## 3. 专家复查三窗口 / Expert pass, three windows

1. **只看译文**：先不看原文，朗读译文，评估是否自然、顺读、有节奏。
2. **对照忠实度**：再对照原文逐句核实意义、语气、隐含信息无增删。
3. **一词多义回查**：在三个窗口回查每个待回查词——
   (a) 句/段局部，(b) 整章语义弧，(c) 后文出现处。
然后重建译文散文质量，最后闭环：`polysemy_unresolved_count` 必须为 0。

## 4. 机器可读闭环字段 / Machine-readable closure fields

写入 `qa/chapter_controls/{NNN_slug}.control.md`：

```
expert_translation_skill_used: true
expert_level_review_status: "PASS"
polysemy_translation_stage_review: "PASS"
polysemy_context_review: "PASS"
polysemy_unresolved_count: 0
```

## 5. 回填 / Backfill

把可复用的经验合并回填到 `skills/translation-quality-defect-families/SKILL.md`。
