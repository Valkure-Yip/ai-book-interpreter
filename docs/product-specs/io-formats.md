# I/O Formats

## 输入格式

### v0.1 支持

| 格式 | 扩展名 | 备注 |
| --- | --- | --- |
| 纯文本 | `.txt` | UTF-8；其他编码自动检测（chardet） |
| EPUB | `.epub` | EPUB 2 / EPUB 3 |
| PDF | `.pdf` | 文本型 PDF；扫描版需 `--ocr` |

### v0.2+ 路线

- `.docx`（学术草稿常见）
- `.tex`（LaTeX 源文件，理想情况——结构最规范）
- `.html` / 网址（在线书籍）
- `.mobi` / `.azw3`（先转 EPUB）

### 输入元数据

CLI 可显式提供（覆盖自动检测）：
```bash
abi translate book.pdf \
  --title "The Structure of Scientific Revolutions" \
  --authors "Thomas S. Kuhn" \
  --source-lang en \
  --target zh
```

## 输出格式

### v0.1：Markdown only

四种 mode：

| mode | 文件名 | 内容 |
| --- | --- | --- |
| `translated` | `translated.md` | 仅目标语言 |
| `bilingual` | `bilingual.md` | 源/译段落对照 |
| `annotated` | `annotated.md` | 译文 + 章节摘要 + 思维导图 + 术语表 |
| `survey-only` | `survey.md` | 仅 Pass 1 产物（不翻译） |

可通过 `--mode translated,bilingual,annotated` 一次性产出多种。

每次运行还会产出：
- `report.md`：质量与成本报告
- `glossary.json` / `glossary.md`：术语表（机器+人类版本）
- `mindmap.mmd`：Mermaid 思维导图源码

### v0.2+ 路线

- EPUB 输出（带 nav.xhtml，可直接读）
- DOCX 输出（带样式）
- PDF 输出（通过 typst 或 pandoc + LaTeX）
- HTML 单文件（嵌入 mermaid 渲染）
- 思维导图独立图像（PNG/SVG，通过 mermaid CLI）

### 输出目录布局

用户指定 `-o ./out`：
```
./out/
├── translated.md
├── bilingual.md
├── annotated.md
├── report.md
├── glossary.md
├── glossary.json
├── mindmap.mmd
└── survey/
    ├── overview.md
    ├── style-guide.md
    └── chapters/...
```

如果用户指定 `-o book.zh.md`（单文件），则**只产出**该文件（隐含 `--mode translated`，其他工件在 `runs/<book-id>/<run-id>/` 中保留）。

## 编码与规范化

- 全部输出 UTF-8，无 BOM，LF 换行
- 中文标点：默认全角；用户可 `--punctuation=half|full|preserve`
- 数字：默认保留原文形式；用户可 `--number-style`

## Markdown 方言

默认 **CommonMark + 部分 GFM 扩展**：
- 表格、删除线、任务列表、围栏代码块
- 脚注：`[^id]` 语法
- 数学：`$...$` 与 `$$...$$`（KaTeX 兼容）
- mermaid：在围栏 ```mermaid 中嵌入

可通过 `--md-flavor=commonmark|gfm|pandoc` 切换。

## 大型书籍切分

`--split-by-chapter` 时，输出按章节切分多个 .md 文件：
```
./out/translated/
├── 00-front-matter.md
├── 01-chapter-1.md
├── 02-chapter-2.md
└── ...
```
配合 `index.md` 汇总目录。
