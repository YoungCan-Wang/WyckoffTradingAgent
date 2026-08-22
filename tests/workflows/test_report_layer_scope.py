"""Tests for regime/trigger layer tables disclosing their scope.

「市场周期分层」与「信号类型分层」的数据来自 `read_trades(best.trades_path)`，
即**单个最优参数单元**的成交，而非全部单元汇总。此前标题未说明，导致误读：

报告曾显示 NEUTRAL 14 笔 +3.80%，而 recent_6m 全部 9 个 trades 文件里 NEUTRAL 实为
0 笔（该档在禁买名单内）——那 14 笔来自 best 所指的另一个周期。我据此一度怀疑
NEUTRAL 禁买的正确性，属被报告口径误导。

不改成聚合全部单元，是因为各周期表现差异极大（recent_6m +24% vs
sideways_2023 -10.78%），混合平均反而更误导。
"""

from __future__ import annotations

from workflows.backtest_market_report_builder import (
    _build_regime_stats_table,
    _build_trigger_stats_table,
)

_REGIME_STATS = [
    {
        "key": "CAUTION",
        "count": 14,
        "first_date": "2026-02-25",
        "last_date": "2026-07-01",
        "win_rate": 50.0,
        "avg": 3.80,
        "median": 0.0,
    }
]
_TRIGGER_STATS = [{"key": "spring(确认)", "count": 8, "win_rate": 50.0, "avg": 5.50, "median": 2.20}]


class TestRegimeTableScope:
    def test_discloses_scope_when_given(self):
        scope = "（recent_6m / 集中换股 / 10天 / SL-8% / 无TP / Trail-8% / 24 笔）"
        text = "\n".join(_build_regime_stats_table(_REGIME_STATS, scope))
        assert "仅统计最优单元" in text
        assert "非全部参数单元汇总" in text
        assert "recent_6m" in text
        assert "24 笔" in text

    def test_still_renders_without_scope(self):
        """scope 缺省时不得崩溃，仍要保留口径提示。"""
        text = "\n".join(_build_regime_stats_table(_REGIME_STATS))
        assert "仅统计最优单元" in text
        assert "| CAUTION |" in text

    def test_data_rows_intact(self):
        text = "\n".join(_build_regime_stats_table(_REGIME_STATS, "（x）"))
        assert "14" in text
        assert "+3.80%" in text


class TestTriggerTableScope:
    def test_discloses_scope(self):
        text = "\n".join(_build_trigger_stats_table(_TRIGGER_STATS, "（bear_2022 / 42 笔）"))
        assert "仅统计最优单元" in text
        assert "bear_2022" in text

    def test_data_rows_intact(self):
        text = "\n".join(_build_trigger_stats_table(_TRIGGER_STATS, "（x）"))
        assert "spring(确认)" in text
        assert "+5.50%" in text


class TestBacktestGateFallbackMatchesLive:
    def test_all_three_workflows_agree(self):
        """回测与两个生产 workflow 的 ALLOW fallback 必须一致。

        PR #305 把 RISK_ON 移出豁免时漏改 backtest_grid.yml，使 run 32537955220
        的回测仍放行 RISK_ON 并产生 6 笔成交（胜率 16.7%、均收 -5.03%）。
        """
        import re
        from pathlib import Path

        values = {}
        for name in ("backtest_grid.yml", "wyckoff_funnel.yml", "step4_from_supabase.yml"):
            text = Path(".github/workflows").joinpath(name).read_text(encoding="utf-8")
            hit = re.search(r"STEP4_BUY_ALLOW_REGIMES:\s*(.+)", text)
            assert hit, name
            raw = hit.group(1)
            # 取 fallback（`|| '...'`）或直接值，去掉引号
            fb = re.search(r"\|\|\s*'([^']*)'", raw)
            values[name] = (fb.group(1) if fb else raw.strip().strip('"').strip("'")).strip()

        assert len(set(values.values())) == 1, values
        assert "RISK_ON" not in next(iter(values.values()))
