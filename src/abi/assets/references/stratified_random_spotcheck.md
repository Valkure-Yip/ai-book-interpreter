# 分层随机抽检 / Stratified Random Spot-Check

> 第一版 EPUB 后强制执行。每轮精校后重抽。
> Mandatory after the first full EPUB and after each refinement pass.

## 抽样总体 / Population

抽样总体 `N` 是**读者可见审计单元**总数，不是页数也不是正文段落数。审计单元分层：

- `paragraph`
- `table`
- `figure`
- `formula`（公式 / 证明块）
- `caption_note`（图注 / 表注 / 注释）

## 抽样预算 / Sampling budget (per round)

- 正文段落：每个 Agent 每轮 120。
- 表格、图片：`N <= 80` 全检，否则每轮总抽 20。
- 公式 / 证明块：`N <= 100` 全检，否则每轮总抽 20。
- 图注 / 表注 / 注释：`N <= 120` 全检，否则每轮总抽 20。

## 两个独立 Agent / Two independent agents

两个 Agent 互不参考，各自对每个样本给出 0-100 分、问题类型、优先级、是否返工、理由。

## 退出条件 / Exit conditions

- 每个 Agent：`average_score >= 92`、`lowest_score >= 88`、无单项 `< 80`、无未关闭 P0/P1/P2。
- `release_confidence = min_h confidence_h >= 0.80`。
- 当前运行连续 PASS 轮次 `current_run_pass_rounds_count >= current_run_pass_rounds_required`
  （默认 required = 2）。旧轮次只能作为历史证据，不计入本次退出。
- 任一样本发现问题：立即归纳问题族 + 全书同类审计 + 闭环，使用新 seed 复抽。
