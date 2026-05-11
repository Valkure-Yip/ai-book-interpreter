# Assembly Design (Pass 3)

> Pass 3：把 IR + Survey + TranslationUnit 装配成最终 Markdown 输出。

## 渲染器架构

```
src/assemble/
├── __init__.py
├── renderer.py        # Renderer 基类
├── translated.py      # 仅译本
├── bilingual.py       # 双语对照
├── annotated.py       # 增强版
└── components/        # 可复用组件
    ├── heading.py
    ├── paragraph.py
    ├── footnote.py
    ├── citation.py
    └── glossary.py
```

每个 Renderer 实现：
```python
class Renderer(Protocol):
    name: str
    def render(self, book: Book, survey: SurveyArtifacts,
               translations: TranslationStore) -> str: ...
```

`assemble.compose(mode, ...)` 根据 mode 路由到对应 Renderer。

## 模式细节

### `translated`

仅目标语言版本。结构：
```
# {title}

> 作者：{authors}

[toc]

# Part I {part_title}
## Chapter 1 {chapter_title}

{paragraph1_translated}

{paragraph2_translated}

...

---

## 译者机器说明

由 AI Book Interpreter v{ver} 生成，模型 {model}。
共 {N} 段，flagged {M} 段。详见 report.md。
```

### `bilingual`

两种子布局，由 `--bilingual-layout=interleaved|side-by-side` 控制。

**interleaved**（默认）：
```
# Chapter 1 {chapter_title_translated}

> **EN:** {paragraph1_source}
>
> **ZH:** {paragraph1_translated}

> **EN:** {paragraph2_source}
>
> **ZH:** {paragraph2_translated}
```

**side-by-side**：HTML 表格（在 Markdown 中嵌入），两列。
```
<table><tr><th>原文</th><th>译文</th></tr>
<tr><td>{p1_source}</td><td>{p1_translated}</td></tr>
...
</table>
```

side-by-side 仅在用户明确要求时使用（Markdown 渲染器兼容性差）。

### `annotated`

`translated` + 以下增强：
- 每个 chapter 标题下插入 `> **本章要点**` 块（来自 `ChapterSummary.key_points`）
- 全书末尾附录：
  - `## 全书概要`（BookOverview）
  - `## 思维导图`（mermaid 代码块）
  - `## 术语表`（按字母/拼音序）
  - `## 翻译质量报告`（confidence 分布、flagged 列表）

### `survey-only`

不依赖 Pass 2 输出，仅产出 Pass 1 工件的人类友好版本。用于"我只想要这本书的导读和思维导图"。

## 关键组件

### Paragraph 渲染

按 `kind` 路由：

```python
def render_paragraph(p: Paragraph, unit: TranslationUnit | None, mode: Mode) -> str:
    if p.kind == "code":
        lang = p.attrs.get("code_lang", "")
        return f"```{lang}\n{p.source_text}\n```"
    if p.kind == "equation":
        return f"$$\n{p.source_text}\n$$"
    if p.kind == "quote":
        text = unit.translated_text if unit else p.source_text
        return "\n".join("> " + line for line in text.splitlines())
    if p.kind == "list_item":
        text = unit.translated_text if unit else p.source_text
        return f"- {text}"
    if p.kind == "figure_caption":
        text = unit.translated_text if unit else p.source_text
        return f"> *Figure: {text}*"
    if p.kind == "footnote":
        return ""  # 在文档末尾统一渲染
    return unit.translated_text if unit else p.source_text
```

### Footnote 收集

遍历过程中收集所有 `kind == "footnote"` 的段落，章末或全书末（按 `--footnote-position` 配置）以 Markdown 脚注语法输出：

```
[^fn-001]: 这是脚注译文
```

正文中的 anchor 转换为 `[^fn-001]`。

### Anchor 处理

源段中的 `[12]`、`Fig. 3.1` 在 LLM 翻译时已被指示**字面保留**。装配时：

- 如果该 anchor 对应到 IR 的某个 footnote/figure/section → 转 Markdown 链接
- 否则 → 保留字面值

由 `tools/lint/anchor_lint.py` 装配前校验：所有 anchor 都能解析。

### Glossary 附录

按目标语言排序输出表格：

```markdown
## 术语表

| 原文 | 译文 | 释义 |
| --- | --- | --- |
| embodiment | 具身 | ... |
| ...
```

`annotated` 模式 + 用户启用 `--include-glossary`（默认 true）。

## 报告生成（`report.md`）

每次装配都产出 `report.md`：

```markdown
# 翻译报告

## 基本信息
- 书名：...
- 原作者：...
- 源语言 → 目标语言：en → zh
- 模型 / Provider：gpt-5 / openai
- Run ID：...
- 耗时：...
- 总成本：$...

## 段落统计
- 总段落数：3,421
- 已翻译：3,398
- 跳过（code/equation）：21
- Flagged：2
  - term_drift: 1
  - low_confidence: 1

## Confidence 分布
- ≥ 0.9: 2,981 (87.7%)
- 0.7-0.9: 412 (12.1%)
- < 0.7: 5 (0.2%)

## Flagged 段落清单
- ab12...-00731 [term_drift]: "..."
- ...

## Token 消耗
- Pass 1: ...
- Pass 2: ...
- Pass 3: ... (零，纯本地)

## 建议
- 5 段 confidence < 0.7，建议人工复核
- glossary 中有 3 个 pending term 未处理
```

## 不变量

1. 每个源段落要么有渲染输出，要么有显式 `skipped` 注释（HTML comment 形式）
2. anchor 必须全部可解析（装配前 lint）
3. 输出 Markdown 必须通过 `markdown-it` 解析无错
4. mermaid 代码块必须通过 mermaid CLI 校验
5. `bilingual` 模式下，源/译段落数严格相等

## 性能

Pass 3 是纯本地、无 LLM、无网络。一本 30 万字书的装配 < 5 秒。
