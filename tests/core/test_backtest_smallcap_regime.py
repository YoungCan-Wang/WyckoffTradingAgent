"""Tests for smallcap benchmark plumbing in backtest replay.

回测此前把 `smallcap_df` 硬传 None（core/backtest_replay._analyze_market_regime），
导致 tools/market_regime.py 里依赖小盘的判据永远不成立：
- CRASH 的 crash_small_day_drop_pct
- PANIC_REPAIR 的 panic_repair_small_rebound_pct

后果是生产库中 CRASH 13 天 / RISK_OFF 15 天 / PANIC_REPAIR 4 天 / RISK_ON 5 天，
在回测里全部塌成 NEUTRAL 或 CAUTION——24 个重叠日仅 14 天判定一致（58%）。
实测 2026-07-28：小盘当日 -7.35%（阈值 -2.5%），smallcap=None 判 NEUTRAL、
传入后判 RISK_OFF。

这直接影响任何按水温分档的回测结论：防守档流动性门槛改动因该档在回测中从不出现
而完全测不出差别（两组指标一字不差）。
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.backtest_execution import ExitSimulationConfig
from core.backtest_replay import BacktestReplayConfig, _analyze_market_regime
from core.wyckoff_engine import FunnelConfig


def _index_frame(pct_chg: float, rows: int = 30) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-06-01", periods=rows, freq="B").date,
            "close": [100.0] * rows,
            "pct_chg": [0.0] * (rows - 1) + [pct_chg],
        }
    )


def _config(**overrides) -> BacktestReplayConfig:
    """BacktestReplayConfig 有 18 个必填字段，这里给出与本用例无关的最小占位。"""

    base = dict(
        trading_days=250,
        hold_days=5,
        board="all",
        top_n=5,
        selection_mode="tradeable_l4",
        full_formal_l4_max=50,
        regime_filter=False,
        execution_regime_gate="live",
        pending_mode="only",
        pending_merge_order="confirmed_first",
        abc_filter=False,
        entry_price_mode="open",
        entry_price_time="",
        entry_price_fallback="close",
        buy_friction_pct=0.026,
        sell_friction_pct=0.176,
        max_atr_hold_days=0,
        exit=ExitSimulationConfig(
            exit_mode="close_only",
            stop_loss_pct=0.0,
            take_profit_pct=0.0,
            trailing_stop_pct=0.0,
            trailing_activate_pct=0.0,
            sltp_priority="stop_first",
            atr_period=14,
            atr_multiplier=2.0,
            atr_hard_stop_pct=0.0,
        ),
    )
    base.update(overrides)
    return BacktestReplayConfig(**base)


class TestAnalyzerReceivesSmallcap:
    def test_smallcap_slice_is_forwarded(self):
        """核心回归：analyzer 必须收到小盘切片，而非 None。"""
        seen: dict[str, object] = {}

        def spy(bench, smallcap, cfg, **kwargs):
            seen["smallcap"] = smallcap
            return {"regime": "NEUTRAL"}

        small = _index_frame(-7.35)
        config = _config(market_regime_analyzer=spy)
        _analyze_market_regime(_index_frame(-1.5), FunnelConfig(), {}, config, small)

        assert seen["smallcap"] is not None
        assert isinstance(seen["smallcap"], pd.DataFrame)

    def test_none_smallcap_still_supported(self):
        """旧快照没有该文件时不得崩溃，只是判据退化。"""
        seen: dict[str, object] = {"called": False}

        def spy(bench, smallcap, cfg, **kwargs):
            seen["called"] = True
            seen["smallcap"] = smallcap
            return {"regime": "NEUTRAL"}

        config = _config(market_regime_analyzer=spy)
        result = _analyze_market_regime(_index_frame(-1.5), FunnelConfig(), {}, config, None)

        assert seen["called"] is True
        assert seen["smallcap"] is None
        assert result["regime"] == "NEUTRAL"

    def test_breadth_is_still_passed_through(self):
        seen: dict[str, object] = {}

        def spy(bench, smallcap, cfg, **kwargs):
            seen["breadth"] = kwargs.get("breadth")
            return {"regime": "NEUTRAL"}

        config = _config(market_regime_analyzer=spy)
        _analyze_market_regime(_index_frame(0.0), FunnelConfig(), {"ratio": 12.0}, config, _index_frame(-3.0))

        assert seen["breadth"] == {"ratio": 12.0}

    def test_defaults_to_none_when_omitted(self):
        """省略参数时保持向后兼容。"""
        seen: dict[str, object] = {}

        def spy(bench, smallcap, cfg, **kwargs):
            seen["smallcap"] = smallcap
            return {"regime": "NEUTRAL"}

        config = _config(market_regime_analyzer=spy)
        _analyze_market_regime(_index_frame(0.0), FunnelConfig(), {}, config)

        assert seen["smallcap"] is None


class TestConfigField:
    def test_replay_config_carries_smallcap(self):
        frame = _index_frame(-3.0)
        assert _config(smallcap_bench_df=frame).smallcap_bench_df is not None

    def test_replay_config_default_is_none(self):
        assert _config().smallcap_bench_df is None


class TestSnapshotLoader:
    def test_missing_file_returns_none(self, tmp_path):
        from workflows.backtest_data import load_snapshot_smallcap_benchmark

        assert load_snapshot_smallcap_benchmark(tmp_path) is None

    def test_reads_smallcap_csv(self, tmp_path):
        from workflows.backtest_data import load_snapshot_smallcap_benchmark

        _index_frame(-4.2).to_csv(tmp_path / "benchmark_smallcap.csv", index=False)
        out = load_snapshot_smallcap_benchmark(tmp_path)
        assert out is not None
        assert "close" in out.columns
        assert out.pct_chg.iloc[-1] == pytest.approx(-4.2)

    def test_does_not_confuse_main_and_smallcap(self, tmp_path):
        """两个文件必须各读各的，不能因缺小盘而回退到大盘。"""
        from workflows.backtest_data import load_snapshot_benchmark, load_snapshot_smallcap_benchmark

        _index_frame(-1.0).to_csv(tmp_path / "benchmark_main.csv", index=False)
        assert load_snapshot_benchmark(tmp_path) is not None
        assert load_snapshot_smallcap_benchmark(tmp_path) is None
