"""回测周期登记表的回归用例。

守的是同一类事故：backtest_grid.yml 的 matrix 加了周期，而报表侧的周期名单没跟着改。
漏改不抛错，只是 period_key 变成空串，接着 walk_forward 把空 key 整段丢掉 ——
2026-08-21 加 bull_2025 后，run 32537955220 的 walk_forward 出现 0 次 bull_2025，
而它是那轮唯一 +29.78% 的周期。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from core.backtest_periods import (
    BACKTEST_PERIODS,
    PERIOD_LABELS,
    period_key_from_dates,
    period_key_from_dirname,
    period_rank,
    resolve_period_key,
)

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/backtest_grid.yml"


def _matrix_period_keys() -> list[str]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    include = data["jobs"]["grid"]["strategy"]["matrix"]["include"]
    return [str(entry["period_key"]) for entry in include]


def test_registry_covers_every_matrix_period() -> None:
    """matrix 里的每个 period_key 都必须能被登记表解析出来。

    这条用例就是那次漏改的检测器：只要 matrix 新增周期而登记表没同步，这里立刻红。
    """
    for key in _matrix_period_keys():
        dirname = f"backtest-grid-{key}-h10-sl8-tp0-tr0"
        assert period_key_from_dirname(dirname) == key, key
        assert PERIOD_LABELS.get(key), key


def test_bull_2025_resolves_from_dirname() -> None:
    # 旧正则的名单停在 volatile_2024，bull_2025 会解析成空串。
    assert period_key_from_dirname("backtest-grid-bull_2025-h10-sl8-tp0-tr0") == "bull_2025"


def test_fixed_ranges_map_back_to_their_key() -> None:
    for period in BACKTEST_PERIODS:
        if period.start and period.end:
            assert period_key_from_dates(period.start, period.end) == period.key


def test_resolve_falls_back_to_dates_when_dirname_unknown() -> None:
    assert resolve_period_key("some-other-dir", "2025-01-02", "2025-12-31") == "bull_2025"
    assert resolve_period_key("some-other-dir", "", "") == ""


def test_period_rank_orders_registry_and_sinks_unknown() -> None:
    assert period_rank("recent_6m") < period_rank("bull_2025")
    assert period_rank("not_a_period") > period_rank("custom")
