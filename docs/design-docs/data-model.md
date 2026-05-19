# Data Model

> 所有跨 pass 的数据契约。**这是单一真相源**——pydantic 模型直接由本文件驱动生成（见 `docs/generated/schemas.md`）。

## 0. 设计原则

1. **不可变**：所有模型默认 frozen；变更走"产生新版本"路径。
2. **可序列化**：每个模型都能 `.model_dump_json()` 落盘，再 `.model_validate_json()` 还原，**字节级稳定**（字段顺序固定）。
3. **稳定 ID**：所有有 ID 的实体，ID 都是输入内容的**纯函数**。
4. **显式 kind**：所有联合类型用显式 `kind` 字段做 discriminator，禁止靠 duck typing。

## 1. Book IR

### 1.1 BookMeta

```python
class BookMeta(BaseModel):
    book_id: str            # sha1(原书规范化字节流)[:12]
    title: str
    authors: list[str]
    source_language: str    # ISO 639-1，自动检测可覆盖
    source_format: Literal["txt", "epub", "pdf"]
    source_path: str        # 绝对路径或 URL
    source_sha256: str      # 完整文件哈希，用于审计
    detected_at: datetime
    notes: list[str] = []
```

### 1.2 Section

```python
class Section(BaseModel):
    section_id: str             # 稳定，见 §3
    level: int                  # 1=part, 2=chapter, 3=section, ...
    heading: str
    heading_trail: list[str]    # ["Part II", "Chapter 5", "5.2 The X"]
    paragraphs: list[Paragraph]
    children: list[Section]     # 嵌套
```

### 1.3 Paragraph

```python
ParagraphKind = Literal[
    "prose",         # 普通正文
    "heading",       # 标题（在 Section.heading 也存，但内联出现时也用此 kind）
    "quote",         # 引用块
    "list_item",     # 列表项
    "code",          # 代码块（保持原文）
    "equation",      # 数学公式（保持原文，可选译标签）
    "figure_caption",
    "table_cell",
    "footnote",
    "citation",      # 单独的引文条目（参考文献列表项）
]

class Paragraph(BaseModel):
    paragraph_id: str         # 见 §3
    kind: ParagraphKind
    source_text: str
    position: int             # 全书内的全局递增序号
    section_id: str           # 反向指针
    anchors: list[Anchor] = []  # 内联引用、脚注 ref、图引用
    attrs: dict[str, str] = {}  # kind 专属属性，e.g. {"code_lang": "python"}
```

### 1.4 Anchor

```python
class Anchor(BaseModel):
    kind: Literal["footnote", "citation", "figure", "section", "url"]
    target: str            # 目标 ID 或 URL
    span: tuple[int, int]  # 在 source_text 中的字符区间
    label: str | None      # 显示文本，如 "[12]"、"Fig. 3.1"
```

### 1.5 Book

```python
class Book(BaseModel):
    meta: BookMeta
    toc: list[Section]              # 根级 sections
    footnotes: dict[str, Paragraph] # footnote_id -> Paragraph
    references: list[Paragraph]     # 参考文献条目，kind=citation
    figures: dict[str, FigureRef]   # figure_id -> 元数据
```

## 2. Survey 工件（Pass 1 产物）

### 2.1 Glossary

```python
class GlossaryEntry(BaseModel):
    term: str                      # 源语言术语，规范化（lowercase + 单复数归一）
    surface_forms: list[str]       # 出现过的所有形式
    target: str                    # 目标语言译法
    alt_targets: list[str] = []    # 候选译法（记录但不使用）
    definition: str                # 简短释义（≤80 字）
    first_seen: str                # paragraph_id
    locked: bool = True            # 是否锁定；锁定后 Pass 2 必须遵守
    source: Literal["survey", "translator-proposed", "human"] = "survey"

class Glossary(BaseModel):
    book_id: str
    entries: list[GlossaryEntry]
    version: int                   # 每次修订递增
```

### 2.2 ChapterSummary

```python
class ChapterSummary(BaseModel):
    section_id: str
    one_liner: str          # ≤60 字
    abstract: str           # ≤300 字
    key_points: list[str]   # 3-7 条
    key_terms: list[str]    # 引用 GlossaryEntry.term
    open_questions: list[str] = []
```

### 2.3 BookOverview

```python
class BookOverview(BaseModel):
    book_id: str
    thesis: str                 # 全书核心论点
    target_audience: str
    register: Literal["academic-formal", "academic-accessible",
                      "popular-science", "textbook"]
    tone_notes: str             # 自由文本，描述语气特点
    chapter_summaries: list[ChapterSummary]
    mindmap_mermaid: str        # Mermaid 思维导图源码
```

### 2.4 StyleGuide

```python
class StyleGuide(BaseModel):
    book_id: str
    target_language: str
    register_directives: list[str]  # 给 LLM 的具体指令，逐条
    forbidden_patterns: list[str]   # 禁用词/短语
    preferred_patterns: list[str]   # 优先句式举例
    quote_style: Literal["「」", """""", "\"\""]
    number_style: Literal["arabic", "cjk-when-low", "preserve"]
```

## 3. ID 算法（不变量）

### 3.1 段落 ID

```python
def paragraph_id(text: str, position: int) -> str:
    canonical = unicodedata.normalize("NFKC", text).strip()
    canonical = re.sub(r"\s+", " ", canonical)
    h = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]
    return f"{h}-{position:06d}"
```

**性质**：
- 纯函数：相同 `text + position` → 相同 ID
- 短：10 字符哈希 + 6 字符位置 = 17 字符
- 可读：position 后缀让人类扫一眼能猜出书中位置

### 3.2 Section ID

```python
def section_id(heading_trail: list[str]) -> str:
    canonical = " > ".join(s.strip() for s in heading_trail)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
```

### 3.3 Book ID

```python
def book_id(source_bytes: bytes) -> str:
    return hashlib.sha1(source_bytes).hexdigest()[:12]
```

## 4. Translation 工件（Pass 2 产物）

### 4.1 TranslationUnit

```python
class TranslationUnit(BaseModel):
    paragraph_id: str           # FK → Paragraph
    kind: ParagraphKind         # 复制自源段落，用于装配
    source_text: str            # 复制，便于独立消费
    translated_text: str
    target_language: str

    terms_used: list[TermUsage]
    confidence: float           # 0..1，由 LLM 自评 + 启发式融合
    flags: list[QualityFlag] = []
    notes: str = ""             # 译者笔记（LLM 产出）

    prompt_version: str         # e.g. "translate-paragraph@v3"
    model: str
    provider: str
    token_usage: TokenUsage
    cost_usd: float
    latency_ms: int
    retries: int

    context_window: ContextWindowMeta  # 见 sliding-window.md
    created_at: datetime

class TermUsage(BaseModel):
    term: str           # GlossaryEntry.term
    rendered_as: str    # 实际译文
    compliant: bool     # 是否符合 glossary.target

class QualityFlag(BaseModel):
    code: Literal[
        "term_drift", "length_ratio_outlier",
        "schema_error", "refusal_detected",
        "low_confidence", "untranslated_residue",
        "anchor_missing",
        # 合法的非翻译：code block / equation / 空段。translated_text == source_text。
        "passthrough",
        # 硬失败：translator 抛异常或 JSON 解析失败。translated_text == "" (绝不
        # 回填源文 — 那样会让 English-in-Chinese 蒙混过 completeness 检查)。
        # 同时带 schema_error 给出原因。
        "translation_failed",
        "skipped",
    ]
    detail: str

class TokenUsage(BaseModel):
    input: int
    output: int
    cached: int = 0
```

### 4.2 PendingTerm

Pass 2 中 LLM 提议但不在 glossary 的术语候选：

```python
class PendingTerm(BaseModel):
    term: str
    proposed_target: str
    paragraph_id: str         # 首次出现
    rationale: str            # LLM 给的理由
    auto_mergeable: bool      # 启发式判断
```

## 5. Run 工件

```python
class RunManifest(BaseModel):
    run_id: str
    book_id: str
    created_at: datetime
    config: RunConfig
    pipeline_versions: dict[str, str]   # {"survey": "v3", "translate": "v7", ...}
    prompt_versions: dict[str, str]
    provider: str
    model: str
    git_sha: str               # 当前代码提交，便于复现
```

## 6. 版本与兼容

- 每个 pydantic 模型有 `schema_version: int = 1` 字段（默认 1）。
- 破坏性变更必须递增；`ir/` 提供 `migrate(v_from, v_to, data)` 函数。
- `docs/generated/schemas.md` 由 `tools/codegen/dump_schemas.py` 生成，CI 校验与代码同步。

## 7. 不可变量（linter 强制）

1. `Paragraph.paragraph_id == paragraph_id(source_text, position)` 必须恒成立。
2. `Section.children` 中所有 `Section.level == parent.level + 1`。
3. `TranslationUnit.paragraph_id` 必须存在于对应 `Book` 的某个 `Paragraph` 中。
4. `GlossaryEntry.first_seen` 必须是有效 `paragraph_id`。
5. `kind ∈ {code, equation}` 的段落，其 `translated_text == source_text`（默认透传；可被特殊策略覆盖但需显式 flag）。
