# 目标语言质量标准：简体中文 / Target Quality Standard: Simplified Chinese

> 所有源语言 → 简体中文 共用。
> Shared by all source languages translating into Simplified Chinese.

## 核心要求 / Core requirements

- 译文必须忠实、可读、自然。不得机械直译、过度压缩或无依据加戏。
- 翻译调用本身：自然中文正文是第一硬约束。prompt 只保留原文片段、最关键的 5-8 条
  文体规则、当前命中的术语；不混入 release / EPUB / lint / QA 文件 / 版本化规则。
- 翻译调用只输出译文，不输出 QA、解释、术语审计、流程记录。

## 简体中文排版硬检查 / Simplified Chinese typography hard checks

- 不得滥用分号。
- 中文字符之间不得有异常空格。
- 标点使用中文全角标点（，。！？：；“”‘’（）《》）。
- 不得出现 BOM、乱码、替换字符、mojibake。
- 不得把旧纸书页码目录当正文放入 EPUB。

## 术语正文呈现 / Terminology in body text

- 普通名词、器物名、衣物名、材料名、动作名必须译成中文，正文不附原文括注。
- 历史术语、制度名、身份称谓、专业术语、文化负载词不得默认写成 `中文（source term）`；
  正文用可读中文译名，原词/定义/理由放入译注、章末注或术语表，用 `[1]` 注号指向。
- 音译人名可在正文首次自然出现处保留一次源语原名；标题中的出现不计入“首次”。

## 只看中文可读性复查 / Chinese-only readability pass

先不看原文，只读译文评分。中文独立阅读低于 4/5、20 句朗读中明显拗口超过 1 句，或关键句
不断气时，即使事实大体准确也不得继续。
