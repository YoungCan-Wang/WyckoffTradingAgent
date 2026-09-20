# 斐波那契回撤位对比回测（Issue #441 复现）

- 生成时间：2026-09-20T15:26:55（Asia/Shanghai）
- 信号窗口：2024-01-02 ~ 2024-12-31
- 样本模式：**full_universe_2024**（股票数=5334）
- 持有：5 个交易日；往返成本：0.202%
- 数据：TickFlow 日线前复权（adjust=forward）；universe=`CN_Equity_A`

## 结论（按净均值）

**最强回撤位：`0.618`**（排序：0.618 > 0.5 > 0.382）

## 分 level 净收益（扣 0.202% 成本后）

| level | n | 平均 | 中位数 | 胜率 | t |
|---|---:|---:|---:|---:|---:|
| 0.382 | 23179 | +0.73% | -0.48% | 47.2% | 11.61 |
| 0.5 | 18283 | +1.27% | +0.20% | 51.3% | 18.43 |
| 0.618 | 9994 | +1.29% | +0.25% | 51.5% | 13.91 |
| **合计** | 51456 | +1.03% | -0.08% | 49.5% | 24.81 |

## 对照：纯随机基线（同样本股票池）

| 组 | n | 平均 | 中位数 | 胜率 | t |
|---|---:|---:|---:|---:|---:|
| fib 信号 | 51456 | +1.03% | -0.08% | 49.5% | 24.81 |
| 纯随机 | 20000 | +0.20% | -0.72% | 44.9% | 3.26 |

- fib − 随机 = **+0.83pp**

## 方法摘要（对齐 #441）

- swing_high = max high in [t−60, t−1]；swing_low = 该高点之前窗口内最低价
- 要求 (high−low)/low ≥ 20%，且 high 距 t ≤ 30 个交易日
- 信号：low ≤ fib_L 且 close ≥ fib_L；同股同 level 5 日内去重；当日涨幅 ≥ 9.5% 跳过
- 入场 t 收盘，出场 t+5 收盘；卖出日跌停（≤−9.5%）则顺延至 t+8 内非跌停收盘

## Caveats

- Observation/research only; not trading advice.
- Limit-up/down proxied by ±9.5% (20cm boards misclassified).
- Survivor bias: delisted names may be missing from current universe.
- ST not PIT-filtered.
- Sample mode: full_universe_2024

文件：`summary.json`、`data/trades.parquet`、`data/klines/*.parquet`