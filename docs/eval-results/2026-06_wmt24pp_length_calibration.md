# 2026-06 · WMT24++ length-ratio 校准

## 背景

新 eval pipeline 的 L2 段落评分依赖 `length_ratio` 区间。旧默认值来自 `QUALITY_SCORE.md`
（小样本经验起点）。本次用 WMT24++（`google/wmt24pp`，`domain=literary`，post-edit 参考）
对主要语言方向做首次真实校准。

## 方法

- 命令：`abi eval calibrate --dataset "wmt24pp:<config>:literary" --min-samples 30`
- 指标：`len(post_edit_reference) / len(source)`（按字符，whitespace 归一）
- 建议区间 = `[p10, p90]`（robust，不被极端段落拉偏）
- 每方向 206 个 literary segment（剔除 `is_bad_source`）

## Runs

| 方向 | n | p10 | p50 | p90 | 建议区间 [p10,p90] | 旧默认 | 处置 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| en→zh | 206 | 0.270 | 0.342 | 0.428 | [0.27, 0.43] | [0.22, 0.55] | 收紧 |
| en→ja | 206 | 0.392 | 0.459 | 0.617 | [0.39, 0.62] | [0.6, 1.4] | **大幅修正** |
| en→es | 206 | 0.962 | 1.074 | 1.212 | [0.96, 1.21] | （无默认） | 新增 |
| en→fr | 206 | 0.971 | 1.123 | 1.303 | [0.97, 1.30] | （无默认） | 新增 |
| en→de | 206 | 0.971 | 1.120 | 1.314 | [0.97, 1.31] | （无默认） | 新增 |

## Findings

1. **en→ja 旧默认 [0.6, 1.4] 是错的**：真实文学数据中位数仅 0.46、p90 也才 0.62。旧默认会把
   正确的日译大面积误判为「过短」（length_ratio_outlier）。这是校准最有价值的一处纠偏。
2. **en→zh 旧默认偏宽**：真实 p90=0.43 远低于旧上界 0.55；收紧后离群检测更灵敏。
3. **拉丁系目标语（es/fr/de）高度一致**：三者建议区间几乎重合（~[0.97, 1.3]），符合英→罗曼/
   日耳曼语轻微扩张的直觉。

## Fixes

- 校准结果落盘为 `src/abi/eval/assets/length_bands.json`（仅聚合统计，无原文）。
- `mechanical.resolve_band` 优先用该校准 bands，未校准方向回退到 `QUALITY_SCORE.md` 默认。

## 局限

- literary 子集每方向仅 206 段，样本偏小；`QUALITY_SCORE.md` 目标的 ≥500 段尚未达到。
- 仅 en→xx 方向（WMT24++ 只有英文源）。xx→en / 非英源方向需 PAR3 或自建集补。
- 建议区间基于 post-edit 参考，非 ABI 产物；属校准非回归基准。

## Next

- 用户提供黄金集后，对 ABI 实际产物复核这些区间是否仍合理。
- 扩样：纳入 WMT24++ 其它 domain（news/social）观察区间是否随语域漂移。
- 加更多目标方向（en→ar/ko/ru 等）扩充 bands 表。
