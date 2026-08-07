# Ingest Design

> 当前 CLI 支持 TXT 与 EPUB。ingest/split 是 deterministic Action；可选 LLM TOC refinement 只修正
> 可疑的 TXT 章节边界，不拥有业务提交权限。

## 1. Action 边界

```text
source.ingest
  → source/source_manifest.json
  → source/source_text.txt

source.split
  → source/toc.json
  → chapters/src/{lowercase-stem}.md ...
```

`make-book` scaffold 时保存用户输入；Action 执行时先从授权来源读取 frozen bytes，再在写任何 staging
文件前完成解析、hash 与章节渲染。executor 只能写当前 attempt staging；validator 校验 manifest、TOC、
章节 identity 和非空内容后，Committer 才提升到 canonical。

## 2. 格式

| 格式 | 解析 | 当前状态 |
| --- | --- | --- |
| TXT | `chardet` + `abi.ir.txt` | 支持本地 path/URL，输出规范化 UTF-8 |
| EPUB 2/3 | `ebooklib`、BeautifulSoup/lxml | 支持本地 path/URL，按 spine/nav 提取文本和 metadata |
| PDF | 无生产 executor | CLI 拒绝；类型枚举中的历史值不构成支持承诺 |

EPUB bytes 写入权限为 `0600` 的受控临时副本供 parser 使用，正常或异常退出都会删除。解析不重新打开
可能已经变化的原始 path。

## 3. TXT 章节识别

确定性检测按以下信号构建 `Book` IR：

- Part/Chapter/罗马数字/编号前缀；
- marker + 标题双行组合；
- 短全大写独立行；
- Preamble、Introduction、Epilogue、Contents 等常见标题词；
- 空行分段和代码块启发式。

没有显式标题时生成单一 synthetic section 并记录 `no_explicit_chapter_detected`；不因 detector 不确定而
丢弃原文。

## 4. EPUB 解析

按 spine 读取 XHTML，优先使用 nav/heading 结构，映射 prose、heading、quote、list item、code、equation、
figure caption、footnote 等 block kind。顶层 Cover、Title Page、Guide、Contents、Index、Copyright、
Imprint、Colophon 等非正文 scaffolding 被跳过并记录 warning。

标题、作者与语言优先使用显式 CLI 值，其次 EPUB metadata，最后使用文件名/默认值。

## 5. 可选 TOC refinement

当确定性结果出现以下情况时，可运行一次结构化 LLM refinement：

- `no_explicit_chapter_detected`；或
- 只有一个 top-level section 且段落数大于 30。

refiner 最多发送 250 条、每条不超过 120 字符的 heading candidates，不发送整本正文。模型只返回
`{line_number, title, level}`；非法 line number、空响应、调用错误或没有改善都回退到原 deterministic TOC。

refinement 默认开启，可通过配置或 `ABI_TOC_REFINE=0` 关闭。它保留原 paragraph stream 和 paragraph
IDs，只允许 section/TOC grouping 变化。

## 6. Chapter stem

`source.split` 在授权前冻结 `expected_chapters`。每个 stem 必须是可移植的小写 ASCII 标识，最终路径为：

```text
chapters/src/001_chapter_title.md
```

`source/toc.json` 的 ordered slug/src 必须与 frozen `expected_chapters` 精确相等；不得在执行后由 agent
任意新增章节或改变大小写命名空间。

## 7. 校验与失败

硬检查包括：

- source manifest 是 JSON mapping 且带 SHA-256；
- clean source 非空；
- TOC 是 ordered list；
- slug 集合与 expected chapters 精确相等；
- 每个 `chapters/src/` 文件非空；
- expected manifest 无缺失或额外文件；
- staged/canonical 路径、media type、role 与 checksum 绑定正确。

解析错误、invalid TOC 或空章节产生 typed semantic failure；identity、symlink、checksum、receipt 或
canonical conflict 属于 integrity failure，run 必须 `BLOCKED`。
