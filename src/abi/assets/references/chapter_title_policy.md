# 章节标题策略 / Chapter Title Policy

> 适用于目录题名、页面标题、EPUB 导航标签。
> Applies to TOC labels, page titles, and EPUB nav labels.

## 规则 / Rules

- 旧纸书目录常用 `--` 把多个主题连成一个长标题。不得机械翻成一串中文破折号。
  必要时拆为：短目录题名（nav label）、页面主标题、可选副标题。
- 不得把 AI 或译者概括出的解释性说明当成读者可见标题。源章只有编号/罗马数字/
  简单题名时，页面标题通常也只用对应编号或题名；解释性说明放入 `title_note`、
  制作说明或 QA 记录。
- 标题、副标题、导航标签中的人名只用目标语言译名。标题中的出现不计入该人名的
  “正文首次出现”。不得把源语原名或括注原名放进标题。
- 删除旧纸书正文分隔符（`* * * * *`、`*****`、`----`、`---`），不得替换成另一种可见分隔符。

## 机器可读字段 / Machine-readable fields (per chapter)

```
nav_label:        # 短目录题名
page_title:       # 页面主标题
subtitle:         # 可选副标题
title_note:       # 不进入读者可见标题的解释
```
