# Ingest Design (Pass 0 + Pass 0.5)

> txt / epub / pdf 三种输入到统一 `Book` IR 的解析策略。

## 通用流程

```
file → detect_format() → parse_to_blocks() → classify_blocks()
     → group_into_sections() → assign_ids() → validate() → Book (heuristic)
     → [Pass 0.5] LLM toc_refiner → Book (LLM-corrected)
```

Pass 0 仍按下述启发式规则切章并赋稳定 ID；得到 `Book` 之后，**Pass 0.5** 会让一次 LLM 调用扫描候选标题、给出修正后的 TOC，并用相同的 paragraph 流重建 `book.toc`。
段落 ID 是内容哈希，重建过程中保持不变；section ID 因 `heading_trail` 变化而变化（设计如此）。

下游所有阶段（survey/translate/assemble）看到的都是 LLM 修正后的 `book.toc`。

## Pass 0.5: LLM TOC refinement（已实现）

| 字段 | 说明 |
| --- | --- |
| 实现 | `abi.ir.toc_refiner` |
| 触发条件 | `RunConfig.refine_toc=True`（默认）且 `needs_refinement()` 为真：`no_explicit_chapter_detected` warning，或 ≤1 个 section 含 >30 段落 |
| 候选构造 | `extract_candidates(raw_text)`: 从原始文本提取 ≤120 字符、不以句末标点结尾、与空行相邻的短行（≤250 条）|
| LLM 输出 | `TOCResponse{chapters: [{line_number, title, level}]}` — `invoke_structured` 调用，Pydantic 强类型解析 |
| 重建逻辑 | `_rebuild_book()`: 按 `line_number` 定位 heading 在原文中的位置，将段落流分配到对应 section；支持层级嵌套（level>1 挂载到前一个 level=1 下）|
| 失败兜底 | LLM 报错 / 输出空 / `line_number` 全部不合法 / 精修后 section 数未增加 → 返回原 Book 不变 |
| 集成点 | `content.py` 的 `split_chapters()` 在 `ingest()` 后、`split_book_to_chapters()` 前调用 `_maybe_refine_toc()` |
| 事件 | `toc.refinement.applied`（精修成功）/ `toc.refinement.skipped`（未改善）|
| 关闭方式 | `--no-refine-toc` CLI flag 或 `ABI_TOC_REFINE=0` 环境变量 |

## TXT

### 章节识别启发式（Pass 0）

按以下优先级匹配标题（`CHAPTER_PATTERNS` → 结构性检测 → 词汇检测）：

1. **显式模式** (`CHAPTER_PATTERNS`):
   - `PART IV`、`Part 3`（level 1）
   - `Chapter N`、`Chapter XIV`、`第N章`、`第N部分`（level 2）
   - 罗马数字独立行 `III.`、`XIV.`（level 1）
   - 编号前缀 `1.` / `1.1` / `1.1.1`（level 3）
   - 数字 + 大写标题 `5 The X`（level 2）

2. **双行标题合并**: 当 *marker 行*（罗马数字 / 阿拉伯数字 / `Chapter N`）后紧跟全大写或首字母大写的标题行（最多 2 行），且前后均有空行 → 合并为一个 heading。典型 Gutenberg 格式：
   ```
   I.
   BOURGEOIS AND PROLETARIANS
   ```

3. **全大写独立行**: ≤120 字符，前后有空行（level 2）

4. **常见标题词**: `Preamble`、`Introduction`、`Epilogue`、`Contents` 等独立出现在空行之间（level 2）

未命中 → 全书视为单章。落 `no_explicit_chapter_detected` warning 但不阻塞；如启用 Pass 0.5 则触发 LLM 精修。

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
