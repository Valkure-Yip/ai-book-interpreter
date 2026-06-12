# EPUB 资产 / 图表 / 表格 / EPUB Assets, Figures, Tables

## 目录 / Directories

- `assets/figures/` — 最终可发布图表。几何图、示意图、线图优先 SVG。
- `assets/images/` — 封面、影印页局部、照片、扫描图、复杂位图。
- `assets/tables/` — 随 EPUB 附带的结构化表格资源。
- `assets/styles/` — EPUB CSS。
- `source/tables/` — 从原书整理的 CSV/TSV 原始表格数据。

## 硬规则 / Hard rules

- 所有 EPUB 内实际使用的 assets 必须写入 OPF manifest。
- XHTML 中不得出现本机绝对路径、`file://`、Windows 盘符或外链热链接。
- 技术性数值表优先生成 XHTML `<table>`，不得只做成图片。
- `chapters/final/*.md` 是编辑源；构建 EPUB 时必须转换为 XHTML，不得把 Markdown 当
  spine 正文。
- 含图表/表格的章节，必须确认 Markdown 源、XHTML 输出、assets 文件、OPF manifest、
  alt 文本、figcaption/table caption 一致后才能进入最终 EPUB。
