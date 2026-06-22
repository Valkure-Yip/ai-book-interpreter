# Eval Results — Index

> 真实评测的产物日志：每次跑过的数据集 / 配置 / 报告 / finding / 后续 action。
> 区别于 `design-docs/eval-standard.md`（讲标准/指标/判据，不变）与 `eval/<eval_id>/`
> （讲产物，可弃）。这里讲**结论**，是接下来"该改什么"的依据。

写法约定：
- 一份评测 = 一份 markdown，文件名 `YYYY-MM_<short-tag>.md`
- 每份内含：背景 / 方法 / runs 表（metrics）/ findings / fixes / 局限 / next
- 不要把 LLM 原始输出拷进来；只引用 paragraph_id + 一行 diff/rationale

## 已记录的评测

| 日期 | Tag | 数据集 | 主要 finding |
| --- | --- | --- | --- |
| 2026-05 | [news_commentary smoke](./2026-05_news_commentary_smoke.md) | `news_commentary:en-zh:academic-accessible:limit_docs=2` | 短文场景五项修复，ABI 胜率 31.2% → 45.0% |
