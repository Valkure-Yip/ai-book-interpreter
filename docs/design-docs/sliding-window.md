# Sliding-Window Context

> Pass 2 中每次段落翻译的上下文构造算法。
> 这是翻译质量的**主要变量**——超过模型选择。

## 核心问题

翻译第 N 段时，模型需要：
- **前文连续性**：术语怎么译过？语气什么样？刚刚说了什么？
- **后文预判**：作者下一段要展开什么？影响代词消解、术语选择。
- **章节定位**：现在在论证链条的哪一步？
- **全局约束**：哪些术语必须按 glossary 走？整本书的 register？

但**不能**把整本书塞进去。必须**有选择地构造**。

## Context Layout

每次 LLM 调用的 messages 结构如下：

```
[system]
  role + style guide (静态，每次相同；可被 prompt cache 命中)

[user]
  ## Translation Contract
  - target_language: zh
  - register: academic-formal
  - quote_style: 「」

  ## Glossary (slice, N terms)
  | term | target | notes |
  | embodiment | 具身 | locked |
  | ...

  ## Book Context
  - thesis: ...
  - target_audience: ...

  ## Where we are
  - heading_trail: Part II > Chapter 5 > 5.2 The X
  - chapter_abstract: ...
  - position_in_chapter: 47/183

  ## Recently translated (前 K 段)
  ### 前-3 (id=ab12...-00044)
  SOURCE: ...
  TRANSLATED: ...
  ### 前-2 ...
  ### 前-1 ...

  ## Up next (后 J 段，仅原文)
  ### 后+1: ...
  ### 后+2: ...

  ## Translate this paragraph
  paragraph_id: ab12...-00047
  kind: prose
  anchors_to_preserve: ["[12]", "Fig. 5.3"]
  SOURCE:
  <这一段的原文>

  Return JSON matching TranslationUnitOutput schema.
```

## 各部分构造算法

### 1. Glossary Slice（关键优化）

**问题**：完整 glossary 可能数百条，全注入会浪费 token 且分散注意力。

**算法**：
```python
def select_glossary_slice(
    target_paragraph: Paragraph,
    window_paragraphs: list[Paragraph],
    glossary: Glossary,
    max_entries: int = 40,
) -> list[GlossaryEntry]:
    # 1. 必含：在 target + window 中出现 surface_form 的所有 term
    must_include = match_terms(target_paragraph, window_paragraphs, glossary)

    # 2. 章节高频 top-K 中尚未入选的
    chapter_top = top_k_terms_in_chapter(target_paragraph.section_id,
                                          glossary, k=20)

    # 3. 全书核心 top-K（按 Pass 1 标注的核心概念）
    core_top = [e for e in glossary.entries if e.is_core][:10]

    merged = dedup(must_include + chapter_top + core_top)
    return merged[:max_entries]
```

`match_terms` 用 Aho-Corasick 多模式匹配；对中英混合按词形归一（lemmatize + lowercase）。

### 2. Recently Translated Window（前 K 段）

```python
K = config.window.before  # default 3
prev = []
for i in range(1, K+1):
    p = paragraph_at(target.position - i)
    if p is None: break
    unit = load_translation(p.paragraph_id)
    if unit:
        prev.append((p, unit))   # 有译文：注入 source + translated
    else:
        prev.append((p, None))   # 没有：仅注入 source
prev.reverse()
```

**注意**：跨章节边界时，前文窗口可能取自上一章节末尾。这是**有意为之**——很多书的章节衔接处有指代回指。
但若跨章节边界，会附加一行 `[Chapter boundary]` 提示。

### 3. Up Next（后 J 段）

```python
J = config.window.after  # default 2
nxt = []
for i in range(1, J+1):
    p = paragraph_at(target.position + i)
    if p is None: break
    nxt.append(p)   # 仅源文，未来还未翻译
```

后文窗口**不**用译文（因为还没翻），仅用源文做"预读"。

### 4. Chapter Scaffold

- `heading_trail`：从根到当前 section 的标题链
- `chapter_abstract`：从 Pass 1 的 `ChapterSummary.abstract`
- `position_in_chapter`：`N/M`，让模型感知阶段（开头/中段/结尾用词倾向不同）

### 5. Anchors To Preserve

源段中提取的 `[12]`、`Fig. X.Y`、`Eq. (3.4)` 等显式 anchor 字符串，原样列出。
这是给模型的硬要求："这些字符串必须出现在译文里"。

### 6. Special Paragraph Kinds

| kind | 处理 |
| --- | --- |
| `code` | 跳过 LLM 调用，直接复制 source → translated；标 `passthrough` |
| `equation` | 同上 |
| `figure_caption` | 走翻译，但 prompt 加 "保留 'Figure X.Y' 标记" |
| `footnote` | 单独调用：context window 仍是宿主段落周围；标注"这是脚注" |
| `citation` | 不译。但若引用包含可译的标题部分（非英文出版的中译书目），可选译；默认透传 |
| `quote` | 翻译，但 prompt 提示"这是引文，保留语气与立场" |
| `list_item` | 翻译，但维持平行结构（前后 list_item 风格一致） |

## Token 预算

- 默认目标：单次调用 ≤ **6k input tokens**，留给 output 1k。
- 超出时按以下顺序裁剪：
  1. 减少后文窗口 J
  2. 减少 glossary slice 上限
  3. 截断 chapter_abstract
  4. 缩减前文窗口 K（最后才动，因为它最关键）

裁剪过程要落事件：
```json
{"event": "context.trimmed", "paragraph_id": "...",
 "reasons": ["over_budget"], "final": {"K": 2, "J": 1, "glossary": 25}}
```

## Prompt Caching

整个 system message + StyleGuide + 全书概要部分**完全相同**——放在 messages 前部以触发 provider 的 prompt cache。

实际成本可降至 30-50%。CLI 应在 `metrics.json` 中分别记录 `tokens.input.cached` 与 `tokens.input.fresh`。

## ContextWindowMeta（落盘）

每个 `TranslationUnit` 中保留：
```python
class ContextWindowMeta(BaseModel):
    k_before: int
    j_after: int
    glossary_size: int
    crossed_chapter_boundary: bool
    trimmed_reasons: list[str]
    prompt_hash: str        # 完整 prompt 的 sha1，用于回放
    token_budget: int
```

这让任意一段的翻译可以**精确回放**——给定相同的 IR + glossary 版本 + prompt_hash，可还原 prompt。

## 不变量

1. **前文窗口仅注入已翻译的段落的译文**，从不"模拟"未翻译段落的译文。
2. **后文窗口从不注入译文**（即便存在），避免循环依赖。
3. **glossary slice 必须包含 target 段落出现的所有 locked term**——否则属于 bug。
4. **token 估算误差 ≤ 5%**：tokenizer 用真实 provider 的（不是估算）。

## 调参与默认值

```yaml
window:
  before: 3
  after: 2
  glossary_max: 40
  chapter_abstract_max_chars: 600
  token_budget: 6000
  trim_order: [j_after, glossary_max, chapter_abstract, k_before]
```

用户可在 CLI 用 `--window.before=5` 之类的语法覆盖。
