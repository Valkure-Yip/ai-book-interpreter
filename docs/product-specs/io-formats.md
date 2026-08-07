# I/O Formats

## 1. 输入

| 格式 | 来源 | 当前行为 |
| --- | --- | --- |
| TXT | 本地 path 或 HTTP(S) URL | 保存原始 bytes，检测编码，规范化为 UTF-8，再确定性拆章 |
| EPUB 2/3 | 本地 path 或 HTTP(S) URL | 从受控副本解析 spine/内容，保存 source manifest 与目录，再确定性拆章 |

PDF、DOCX、LaTeX、MOBI/AZW3 当前不在 CLI 支持范围。输入不匹配 `.txt` / `.epub` 或无法安全解析时
fail closed，不通过扩展名猜测其他格式。

```bash
abi make-book SOURCE \
  --source-target en-zh-Hans \
  [--title SLUG] \
  [--mode public_domain|licensed|private_use]
```

`--source-target` 是语言对模板；当前默认 `en-zh-Hans`。版权模式决定所需的 evidence 与输出位置，
不改变质量门禁。

## 2. 书籍工程布局

每次 `make-book` 创建一个工程和唯一 durable run：

```text
books/{target}/{NNNN}_{slug}/
├── source/
│   ├── source_text_raw.txt
│   ├── source_text.txt
│   ├── source_manifest.json
│   └── toc.json
├── metadata/
├── glossary/
├── chapters/
│   ├── src/
│   ├── translated/
│   ├── controlled/
│   └── final/
├── qa/
├── reviews/
├── preproduction/
├── output/
│   ├── book.epub
│   ├── publication_lint.json
│   ├── asset_manifest_check.json
│   ├── epubcheck.json
│   ├── final_manifest.md
│   └── release/
├── retrospective/
├── state/
│   ├── run.db
│   ├── graph_checkpoints.sqlite
│   ├── action_checkpoints.sqlite
│   └── staging/{action_id}/{attempt}/
├── events.jsonl
└── metrics.json
```

并非所有文件在 scaffold 时就存在。只有 committed prerequisite 满足后，PolicyEngine 才会授权产生后继
工件的 Action。

## 3. 章节文件

章节文件统一为 UTF-8、LF 换行的 Markdown：

- `chapters/src/{chapter}.md`：受控原文；
- `chapters/translated/{chapter}.md`：初译，不被后继覆盖；
- `chapters/controlled/{chapter}.md`：零问题章控后的修订；
- `chapters/final/{chapter}.md`：忠实度、可读性/意象、术语和章节 gate 通过后的版本。

`chapter` stem 只能使用小写命名空间：`[a-z0-9_.-]+`。同一 stem 贯穿全部目录和 QA 报告，以便
PolicyEngine 精确授权、Scheduler 检测冲突、repair 路由定位受影响章节。

## 4. 最终输出

当前用户产物是 EPUB，而不是旧的单文件 Markdown mode：

| 路径 | 含义 |
| --- | --- |
| `output/book.epub` | 通过构建、publication lint、asset manifest 与 EPUBCheck 的 canonical EPUB |
| `output/release/book_{version}.epub` | 通过最终评审与抽检后的版本化发布副本 |
| `output/release/release_state.json` | latest version、status 与 release metadata |
| `output/final_manifest.md` | 最终 committed 工件清单 |

私人自用模式的受限产物进入 gitignored 的 `output/private_artifacts/`；系统不会把版权模式当作绕过质量
或安全门禁的理由。

## 5. 质量证据

主要证据路径：

```text
qa/chapter_controls/{chapter}.control.md
qa/fidelity/{chapter}.md
qa/readability/{chapter}.md
qa/imagery/{chapter}.imagery.md
qa/terminology/{chapter}.md
qa/gates/{chapter}.gate.md
reviews/agent_a/review.md
reviews/agent_b/review.md
reviews/random_spotcheck/round_NNN/
```

这些文件必须与对应 Action、attempt、manifest digest、checksum 和 gate receipt 绑定；单纯存在于文件系统
不能证明质量通过。

## 6. 编码与安全

- 文本 canonical 输出为 UTF-8、无 BOM、LF 换行；
- 文件名和 artifact path 必须是相对路径，禁止 `..`、绝对路径、NUL 和 symlink traversal；
- 中文最终章节在 staged-write 边界规范化与 CJK 相邻的 ASCII 引号，publication lint 再次拒绝残留；
- EPUB 内部路径、媒体类型、导航、资源清单和 ZIP 结构由确定性 validator 检查。
