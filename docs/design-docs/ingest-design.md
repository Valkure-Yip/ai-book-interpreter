# Ingest Design (Pass 0)

> txt / epub / pdf 三种输入到统一 `Book` IR 的解析策略。

## 通用流程

```
file → detect_format() → parse_to_blocks() → classify_blocks()
     → group_into_sections() → assign_ids() → validate() → Book
```

## TXT

### 章节识别启发式

按以下规则的优先级匹配标题：

1. 全大写整行且长度 < 80（`THE STRUCTURE OF MIND`）
2. 形如 `Chapter N`、`第N章`、`Part N` 开头
3. 编号 + 句号或制表符（`1.`、`2.1 `、`3.1.2 `）
4. 紧邻其前后有空行（标题通常被空行包围）

未命中 → 全书视为单章。落 `structure-warnings.json` 但不阻塞。

### 段落分割

连续非空行 = 一段；空行分隔。
代码块识别：连续行均缩进 ≥ 4 空格 且 含 ASCII 比例高 → `code`。

## EPUB

### 解析器
`ebooklib` 读取 spine + nav，对每个 HTML 文件用 `BeautifulSoup`。

### HTML → Block 映射

| HTML | kind |
| --- | --- |
| `h1`-`h6` | heading（同时记 level） |
| `p` | prose |
| `blockquote` | quote |
| `pre`, `code` (block-level) | code |
| `li` | list_item |
| `figure > figcaption` | figure_caption |
| `a.footnote-ref` / `aside.footnote` | footnote |
| `span.math`, MathML | equation |

### 章节边界
优先用 EPUB 的 nav.xhtml（TOC）切分。若无：按 `h1`/`h2` 启发式。

### 注脚
EPUB 注脚通常分散在文末或独立 xhtml；解析时收集到 `Book.footnotes` 字典，原段落中保留 anchor。

## PDF

### 解析器
PyMuPDF (`fitz`)，按页面提取 `dict` 模式，得到 spans 含字体、字号、bbox。

### 启发式（重头戏）

#### 去噪
- 重复出现在多数页面相同 y 坐标的短文本 → 页眉/页脚，丢弃
- 仅含数字的孤立 block 且贴近上下边距 → 页码，丢弃

#### 段落重组
- 当前行的 baseline 与上一行接近，行末非句号/问号 → 同段续接
- 缩进首行 = 新段落起始
- 字号变化 + 行高变化 → 段落边界

#### 标题识别
- 字号 > 中位数 + N → 候选标题
- 全大写 + 短行 → 候选标题
- 编号前缀（`Chapter`, `1.2`） → 强候选

按字号聚类映射到 heading level（最大 → h1，次大 → h2，…）。

#### 代码 / 等宽
- 字体名含 `Mono`/`Courier` → 该 block 标 `code`
- 行起始有规则缩进 + 高 ASCII 比例 → 候选 code

#### 数学
- 含特殊符号区块（`∑`, `∫`, `≤`, …）或源是嵌入图片 → `equation`
- 若是图片，提取 OCR 结果但保留 `attrs.original_format=image`

### 跨页段落合并

页 N 末段不以句末标点结尾 + 页 N+1 首段不以大写起始 → 合并。

### 失败回退

复杂版式（双栏、混排、表格丰富）的置信度低时：
1. 落 `ir/pdf-confidence.json`
2. 启用 **LLM 辅助分块**（见下文）

### LLM 辅助分块（可选）

对低置信度页面，把每个文本 block 的 `{text, font_size, bbox, page}` 传给 `StructureClassifier` agent，得到 `kind` 标签。
仅用于结构，不用于翻译；token 消耗记入"Pass 0 辅助"账目。

## OCR Fallback

仅当：
- pdf 解析出 < 100 字符 / 页
- 用户显式 `--ocr` flag

使用 Tesseract（默认）或 macOS Vision API（可选）。OCR 文本走 PDF 同一管线，但所有段落带 `attrs.from_ocr=true` 与 `attrs.ocr_confidence`。

## 校验

Pass 0 结束前的硬检查：

- `len(book.toc) >= 1`
- 所有段落 `position` 严格单调递增、连续无空洞
- 所有段落 `paragraph_id` 唯一
- 每个 anchor 的 `target` 要么解析到有效 ID（footnote/figure/section），要么标 `kind="url"`
- `Book.meta.title` 非空（无法识别时用文件名做兜底）

## 不变量

1. Pass 0 不调用任何翻译 LLM（仅可选调用 `StructureClassifier`）
2. Pass 0 是**纯函数**（相同输入 + 相同启发式版本 → 相同输出）
3. Pass 0 的所有启发式参数都在 `config/ingest.yaml`，可被覆盖、可被测试
