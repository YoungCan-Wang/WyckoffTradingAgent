"""回测周期登记表：period_key / 展示名 / 固定日期区间的唯一事实源。

此前 period 名单在四处各写一份（backtest_grid.yml 的 notify 内联脚本、
backtest_market_report_artifacts._parse_period_key、backtest_market_report_builder
的 PERIOD_LABELS、backtest_parameter_stability._period_rank）。matrix 每加一个周期
都要同步四处，实测漏改后果不是报错而是静默失真：

- 2026-08-21 加 bull_2025 后，_parse_period_key 未同步 → period_key 为空串 →
  backtest_walk_forward 第 38 行 ``if cell.period_key`` 直接丢掉该周期。run
  32537955220 的 walk_forward 只有 16 个窗口（4 风格 × 4 迁移），bull_2025 出现
  0 次；而它是最近的完整年度、也是该次唯一 +29.78% 的周期。
- notify 内联脚本的正则同样缺三个周期，三者一起回落到 BT_PERIOD_LABEL 兜底。
  all_defined 下该值是整串逗号列表，于是 sideways_2023 / volatile_2024 / bull_2025
  被并成同一个 bucket，而 _best_cells_by_period 只留每 bucket 最大值，
  sideways_2023 的 -10.78% 被 bull_2025 的 +29.78% 盖住，period_count 从 6 掉到 4，
  足以把 robust_label 抬到「稳健参数（跨周期全正）」。

新增周期只改这里，并同步 backtest_grid.yml 的 matrix 与日期分支。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestPeriod:
    key: str
    label: str
    start: str | None = None
    end: str | None = None


# 顺序即报表展示顺序：滚动窗口在前，历史窗口按时间先后，custom 收尾。
BACKTEST_PERIODS: tuple[BacktestPeriod, ...] = (
    BacktestPeriod("recent_2m", "最近2个月"),
    BacktestPeriod("recent_6m", "最近6个月"),
    BacktestPeriod("bull_2020", "牛市 2020-07~2021-02", "2020-07-01", "2021-02-18"),
    BacktestPeriod("bear_2022", "熊市 2021-12~2022-10", "2021-12-13", "2022-10-31"),
    BacktestPeriod("sideways_2023", "震荡市 2023", "2023-01-03", "2023-12-29"),
    BacktestPeriod("volatile_2024", "高波动市 2024", "2024-01-02", "2024-12-31"),
    BacktestPeriod("bull_2025", "牛市 2025", "2025-01-02", "2025-12-31"),
    BacktestPeriod("custom", "自定义周期"),
)

PERIOD_KEYS: tuple[str, ...] = tuple(period.key for period in BACKTEST_PERIODS)
PERIOD_LABELS: dict[str, str] = {period.key: period.label for period in BACKTEST_PERIODS}
PERIOD_ORDER: dict[str, int] = {period.key: idx for idx, period in enumerate(BACKTEST_PERIODS)}
FIXED_PERIOD_RANGES: dict[tuple[str, str], str] = {
    (period.start, period.end): period.key for period in BACKTEST_PERIODS if period.start and period.end
}

# 目录名形如 backtest-grid-<period_key>-h10-sl8-tp0-tr8
_DIRNAME_PATTERN = "backtest-grid-{key}-h"


def period_key_from_dirname(dirname: str) -> str:
    """从 artifact 目录名解析 period_key；解析不出返回空串。"""
    text = str(dirname or "")
    for key in PERIOD_KEYS:
        if _DIRNAME_PATTERN.format(key=key) in text:
            return key
    return ""


def period_key_from_dates(start: str, end: str) -> str:
    """按固定日期区间反查 period_key；滚动窗口与未知区间返回空串。"""
    return FIXED_PERIOD_RANGES.get((str(start or "").strip(), str(end or "").strip()), "")


def resolve_period_key(dirname: str, start: str = "", end: str = "") -> str:
    """先按目录名解析，再按固定日期区间兜底。"""
    return period_key_from_dirname(dirname) or period_key_from_dates(start, end)


def period_label(key: str) -> str:
    return PERIOD_LABELS.get(str(key or ""), str(key or ""))


def period_rank(key: str) -> int:
    """未登记周期排在末尾，保持既有 9 的哨兵语义。"""
    return PERIOD_ORDER.get(str(key or ""), len(BACKTEST_PERIODS) + 1)
