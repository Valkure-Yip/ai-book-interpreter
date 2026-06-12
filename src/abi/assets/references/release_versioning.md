# 版本化发布 / Release Versioning

> EPUB 按软件版本号发布。`output/book.epub` 只是构建产物，不是最终交付。
> The EPUB is released with a semantic version. ``output/book.epub`` alone is not a deliverable.

## 公开 / 授权项目 / Public or licensed projects

写入 `output/release/`：

- `{目标语言书名}_vX.X.X.epub`（不得平铺在 `output/` 根，不用英文 slug 或通用 `book_` 前缀）
- `release_notes.md`（累计；最新条目插入文件顶部）
- `release_state.json`（`latest_status == PASS` 是公开 `DONE` 的必要条件）
- `release_index.md`（所有版本索引）

## 私人自用项目 / Private-use projects

写入仅限本地、被 Git 忽略的 `output/private_artifacts/`：

- `{目标语言书名}_private_vX.X.X.epub`
- `private_artifact_notes.md`
- `private_artifact_state.json`（`latest_status == PASS`）
- `private_artifact_index.md`

## 版本号 / Version numbers

- 初次发布 `v0.0.1`（或 `v0.1.0`）。
- 内容、排版、metadata、图表、注释或抽检修复变化后，创建新的 patch release。
- 不得覆盖旧版本 EPUB；note 追加到累计文件顶部。

## release note 必含 / Release note must record

发布原因、问题点、修复、QA 证据（随机抽检轮次、`release_confidence`、EPUBCheck、
publication lint）、风险、下一轮迭代。
