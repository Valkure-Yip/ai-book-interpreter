# 私人自用模式 / Private-Use Mode

> 仅当用户提供本地书源并作出明确私人自用声明时使用。严格个人学习自用、非商业、不传播。
> Use only with a user-provided local source and an explicit private-use declaration.
> Strictly personal study use, non-commercial, not redistributed.

## 边界 / Boundary

- 工程必须位于被忽略的 `books/private/{target}/...`，不得进入可发布的 `books/{target}/`。
- 私人原文、译文、QA、EPUB、metadata 不得提交到 GitHub。
- 版本化产物写入被忽略的 `output/private_artifacts/`，不是公开 release。

## 封面与前置页 / Cover & frontmatter

- 制作标识使用：`参考public-domain-books-translation 开源项目 个人自制`。
- 封面不得包含公版来源声明或长版权免责声明。
- 前置页必须说明私人自用边界。

## 版权 / Rights

- 不查找非公版全文。源文件必须由用户本地提供。
- `metadata/rights_checklist.md` 结论使用 `PRIVATE_USE_PASS`，并存在
  `metadata/private_use_declaration.md`。
