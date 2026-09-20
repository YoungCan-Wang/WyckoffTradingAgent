# METHOD — 斐波那契回撤位多年回测（2021–2026）

对齐 Issue #441 口径；本目录为研究归档（结果 + 图），不含 TickFlow 密钥与巨型 parquet 缓存。

## 信号与交易

- 数据：TickFlow 日线前复权（`adjust=forward`）；universe=`CN_Equity_A`
- 样本：`full_universe_2021_2026`（约 5513 只）；信号窗 2021-01-04 ~ 2026-09-18（2026  partial）
- swing_high = max high in [t−60, t−1]；swing_low = 该高点之前窗口内最低价
- 要求 (high−low)/low ≥ 20%，且 high 距 t ≤ 30 个交易日
- 信号：low ≤ fib_L 且 close ≥ fib_L；同股同 level 5 日内去重；当日涨幅 ≥ 9.5% 跳过
- 入场 t 收盘，出场 t+5 收盘；卖出日跌停（≤−9.5%）则顺延至 t+8 内非跌停收盘
- 往返成本 0.202%；levels = {0.382, 0.5, 0.618}

## 本目录文件

| 文件 | 说明 |
|------|------|
| `RESULTS.md` | 中文结果摘要（全样本 / 分年×level / 随机基线） |
| `summary.json` | 机器可读指标 |
| `charts/chart_overall_by_level.png` | 全样本分 level 净均值 |
| `charts/chart_by_year_level.png` | 分年×level 净均值（分组柱） |
| `charts/chart_winrate_by_year.png` | 分年×level 胜率 |
| `charts/chart_compare_2024_vs_multiyear.png` | 多年合计 vs 2024 子样本 vs 2024-only 复现 |

对照：同日 2024-only TickFlow 复现见 `../rerun_2024_tickflow/`。

## Caveats

Observation/research only; not trading advice. Limit-up/down proxied by ±9.5%. Survivor bias / ST not PIT-filtered. 2026 YTD only.
