# The Three-Pass Pipeline

> Pass 0/1/2/3 的完整规范。
> 每个 pass 都是**幂等**、**可恢复**、**可独立运行**的。

## 概览

```
Pass 0: Ingest        原书 → Book IR
Pass 1: Survey        Book IR → Glossary + BookOverview + StyleGuide + Mindmap
Pass 2: Translate     Book IR + Survey → TranslationUnit[]
Pass 3: Assemble      Book IR + Survey + TranslationUnit[] → 各种输出 .md
```

每个 pass 接收**版本化的 JSON/Markdown 工件**作为输入，产出**版本化的 JSON/Markdown 工件**作为输出。
中间不存在内存对象通信。

---

## Pass 0：Ingest（解析与归一化）

### 目标
把异构输入（txt/epub/pdf）规约到统一的 `Book` IR。

### 子步骤

1. **嗅探格式**：按扩展名 + magic bytes 决定走哪条解析路径。
2. **解析**：
   - `txt`：见 [`ingest-design.md#txt`](./ingest-design.md#txt)，启发式识别章节标题。
   - `epub`：解析 spine 与 NCX/nav；HTML → 语义块（h1-h6 → Section；p → prose；pre/code → code；blockquote → quote；li → list_item）。
   - `pdf`：PyMuPDF 抽取 + 布局启发式（去页眉页脚、重组跨页段落）；复杂版式可选启用 **LLM 辅助分块**（见 §0.2）。
3. **检测元数据**：标题/作者/语言（langdetect + epub metadata）。
4. **特殊块识别**：
   - 代码：缩进块、围栏 `\`\`\``、字体启发式（pdf）
   - 公式：LaTeX 模式 `$...$`、`\\[...\\]`、MathML（epub）
   - 引文：`(Author, Year)`、`[12]` 模式
   - 脚注：epub 注脚链接、pdf 上标数字 + 底部对齐
5. **构造 ID**：按 §3 算法计算所有段落、章节、书籍 ID。
6. **校验**：
   - 全书段落总数 > 0
   - 章节层级单调（不跳级）
   - 所有 anchor target 都解析到存在的目标，否则降级为纯文本

### 输出
`runs/<book-id>/<run-id>/ir/book.json`

### 失败模式
- pdf 解析失败 → 降级到 OCR（可选 feature flag）→ 仍失败则人类介入
- 章节识别可疑（仅 1 章或章节数 > 200） → 落盘 `ir/structure-warnings.json`，**不阻塞**后续 pass

### 0.2 LLM 辅助分块（可选）

当 pdf 启发式置信度低于阈值时，对每页文本块切片调用 LLM 进行结构分类（`heading | prose | code | caption | footnote | header_footer_noise`）。仅用于 IR 构造，**不**翻译，**不**进入 token 计费的主路径。

---

## Pass 1：Survey（通读）

### 目标
读完整本书，产出**全局共享的翻译上下文**：术语表、章节摘要、风格指南、思维导图、整体论点。

### 算法（map-reduce）

#### Step 1: Per-chapter map
对每个章节并行调用 LLM：

输入：
- 章节标题路径（`heading_trail`）
- 章节全文（去掉 code/equation 等不译块，仅放文本骨架）
- 紧邻前后章节的标题（仅标题，不放全文）

要求 LLM 输出（结构化）：
```json
{
  "one_liner": "≤60 字",
  "abstract": "≤300 字",
  "key_points": ["..."],
  "key_terms": [
    {"term": "embodiment", "proposed_target": "具身",
     "definition": "...", "first_surface": "embodiment"}
  ],
  "open_questions": ["..."]
}
```

如果章节过长（> max_chunk_tokens），先做章节内段落级 map-reduce：把章节切成 N 块各自摘要，再 reduce 成章节摘要。

#### Step 2: Term aggregation
- 收集所有章节的 `key_terms`
- 同义合并：相同 surface_form 或语义同义（用 embedding 相似度 ≥ 0.92）
- 冲突解决：同一 term 有多个候选 target 时，按 (出现频次, 第一次出现位置) 排序，由"术语仲裁 prompt"做最终决定
- 产出 `Glossary`，全部 `locked=True`

#### Step 3: Global reduce
- 输入：所有 `ChapterSummary` + 全书 toc + glossary
- 输出 `BookOverview`：`thesis`、`target_audience`、`register`、`tone_notes`、`mindmap_mermaid`

#### Step 4: Style guide derivation
- 基于 `register` + 目标语言 + 用户偏好（CLI flag），生成 `StyleGuide`
- 内置 register → directives 的映射模板，LLM 仅做"为这本书定制"的微调

### 输出
```
runs/<book-id>/<run-id>/survey/
├── overview.json
├── overview.md           # 人类可读版
├── chapters/<section_id>.json
├── glossary.json
├── style-guide.json
├── style-guide.md
└── mindmap.mmd           # Mermaid 源码
```

### 不变量
- `Glossary.entries` 中任何 `term` 都对应至少一个 `Paragraph` 含 `surface_form`
- `ChapterSummary.section_id` 全部唯一且覆盖 toc 中所有 `level <= 2` 的 section
- `BookOverview.thesis` 非空

---

## Pass 2：Translate（段落翻译）

### 目标
为每个 `Paragraph` 产出一个 `TranslationUnit`，确保**术语锁定 + 上下文连贯**。

### 调度

```
for chapter in book.toc (并行度 = config.concurrency.chapters):
    for paragraph in chapter.paragraphs (串行, 用滑动窗口):
        if exists(translate/paragraphs/<id>.json) and not force-rerun:
            skip
        ctx = build_context(paragraph, prior_translations, glossary, style)
        unit = llm.translate(ctx)
        unit = validate_and_flag(unit)
        save(unit)
```

**章节内串行**是为了滑动窗口能引用刚翻好的前文。
**章节间并行**因为章节间上下文耦合相对松（且我们已经有 Pass 1 的章节摘要兜底）。

### Context 构造
详见 [`sliding-window.md`](./sliding-window.md)。

简而言之，一次翻译调用的上下文包括：
1. **System**：StyleGuide + 翻译角色定义
2. **Glossary slice**：本段附近会用到的术语条目（不是整张表）
3. **Chapter scaffold**：章节标题路径 + 章节摘要
4. **Window**：前 K 段（源 + 译）+ 后 J 段（仅源）
5. **Target**：要翻译的段落

### 输出 schema

要求 LLM 返回结构化（pydantic 校验）：
```json
{
  "translated_text": "...",
  "terms_used": [
    {"term": "embodiment", "rendered_as": "具身", "compliant": true}
  ],
  "confidence": 0.88,
  "notes": "脚注 ref 已保留",
  "untranslated_passthrough": false
}
```

### 验证 & 标记

每段译完立刻跑：
1. **Schema 校验**（pydantic）
2. **术语合规**：把 source 中出现的 glossary term 对照 `terms_used`，确认 `rendered_as == GlossaryEntry.target`
3. **长度比检查**：`len(translated) / len(source)` 落在 `[ratio_lo, ratio_hi]` 范围（按语言对预设，可在 StyleGuide 覆盖）
4. **Anchor 保留**：source 中的 `[12]`、`Fig. 3.1` 等 anchor 标记必须在译文中**字面保留**
5. **拒答检测**：检测 "I cannot translate" 之类的 LLM refusal 模式
6. **未翻译残留**：用脚本扫描译文是否仍含大量源语言字符（按 unicode block 启发式）

任何一项失败 → 添加 `QualityFlag` 并：
- 若 `retries < max_retries`：用"修订 prompt"（带具体错误）重试
- 否则：保留此次结果，但 `flags` 非空，进入 `flagged.jsonl` 待人工/后续清理

### Pending terms
LLM 提议新术语 → 写入 `translate/pending-terms.jsonl`，**不**立即并入 glossary（避免 race）。

每翻译完 1 章后，运行 `term-merge` 子步骤：
- 高置信（频次 ≥ 3 + 候选译法唯一） → 自动并入 glossary，glossary.version++
- 否则 → 留待 Pass 2 结束时批量仲裁

### 输出
```
runs/<book-id>/<run-id>/translate/
├── paragraphs/<paragraph_id>.json
├── flagged.jsonl
├── pending-terms.jsonl
└── glossary.json         # 可能被 Pass 2 扩展的版本，version > Pass 1 版本
```

### 不变量
- 对 Book IR 中每个 `Paragraph`，要么有 `translate/paragraphs/<id>.json`，要么有 `skipped.jsonl` 记录
- 所有 `TranslationUnit.paragraph_id` 唯一
- `kind ∈ {code, equation}` 默认 `translated_text == source_text`，且 `flags` 中含 `"passthrough"`

---

## Pass 3：Assemble（装配输出）

### 目标
按用户选择的输出模式，从 IR + Survey + TranslationUnit 组装最终 Markdown。

### 模式

| mode | 包含 |
| --- | --- |
| `translated` | 仅目标语言译文 |
| `bilingual` | 段落级双语对照（源在上、译在下，或左右栏） |
| `annotated` | 译文 + 每章 AI 摘要 + 全书概要 + 思维导图 + 术语表附录 |
| `survey-only` | 仅 Pass 1 产物（不需要 Pass 2） |

### 装配规则

1. 按 `Book.toc` 顺序遍历 sections（DFS）
2. 对每个 section 输出 heading（`#` 数对应 `Section.level`）
3. 在 section 起始可选插入 `ChapterSummary`（取决于 mode）
4. 按 `Paragraph.position` 顺序输出段落：
   - `prose` → 译文（或双语块）
   - `code` / `equation` → 围栏 / `$...$`，原样
   - `quote` → `>` 前缀
   - `list_item` → `-` 前缀
   - `figure_caption` → `> **Figure X.Y**: ` + 译文
   - `footnote` → 末尾收集，输出 `[^id]: 译文` 风格的 Markdown 脚注
5. anchors 转换为 Markdown 链接 / 脚注引用
6. 全书末尾附录（仅 `annotated`）：
   - 全书概要
   - 思维导图（mermaid 代码块）
   - 术语表（按字母序）
   - 译者机器笔记（confidence 分布、flagged 段落清单）

### 输出
```
runs/<book-id>/<run-id>/assemble/
├── translated.md
├── bilingual.md
├── annotated.md
└── report.md           # 装配报告：段落数、flagged 数、token/cost 汇总
```

### 不变量
- 装配前先跑 `tools/lint/glossary_lint.py`，glossary 违规数为 0 才允许产出 final
  （违规非 0 时仍可产出 `*.draft.md`，明确标注）
- 每个源段落必须有对应输出片段或 `skipped` 注释
- Markdown 通过 `markdown-it` 解析无错误

---

## Pass 间的依赖与可重入

| 重跑 | 影响 |
| --- | --- |
| Pass 0 | 所有 ID 可能变 → 必须重跑 Pass 1/2/3 |
| Pass 1 | glossary 可能变 → 所有受影响段落的 Pass 2 需重跑（按 glossary diff 选择性重跑） |
| Pass 2 | 仅重跑指定段落即可 |
| Pass 3 | 纯装配，随时可重跑 |

`runtime` 提供 `abi resume` 自动按 manifest 推断需要重跑的部分。
