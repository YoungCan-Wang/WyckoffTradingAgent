"""Tests for money_flow / amount_distribution plumbing in backtest replay.

回测此前只把 breadth 传给 analyzer，money_flow 和 amount_distribution 留空
（core/backtest_replay._analyze_market_regime）。tools/market_regime.py 不报错，
而是拿一个空的行情字典兜底重算：

    money_flow_context = money_flow or calc_market_money_flow({}, breadth)
    amount_distribution_context = amount_distribution or calc_amount_distribution_health({}, ...)

于是 money_flow.sample_size=0、分数退化成跟 breadth 涨跌幅同值、成交额分布是「样本不足」。

CRASH 要求价格判据 + 一条确认判据（广度断崖 breadth_delta<=-20 或资金撤退），
确认永远拿不到，CRASH 就掉到 RISK_OFF。实测生产库 9 天 CRASH 在回测里只剩 1 天
（2026-08-19 广度 delta=-30.98 自己够到断崖阈值，不依赖资金流）。

两档下游不同，所以不是化妆问题：
- workflows/step4_order_engine.py 仓位系数 CRASH 0.70 vs RISK_OFF 0.85
- core/market_trade_mode.py 执行优先级 CRASH 2 vs RISK_OFF 3
- _tune_risk_off_cfg 只认 RISK_OFF，没有 CRASH 分支

注意 RISK_OFF 兜底那条路径返回的 panic_reasons 是空的（_regime_from_panic 返回
regime, [], []），所以空 panic_reasons 不能读成「价格判据没触发」。
"""

from __future__ import annotations

import pandas as pd

from core.backtest_execution import ExitSimulationConfig
from core.backtest_replay import (
    BacktestReplayConfig,
    _analyze_market_regime,
    _calculate_amount_distribution,
    _calculate_market_money_flow,
)
from core.wyckoff_engine import FunnelConfig
from tools.market_regime import analyze_benchmark_and_tune_cfg

# 2026-07-17 的真实形态：主板 -3.046%、小盘 -7.145%，两条价格判据都过
# （阈值 -1.3% / -2.5%），而广度 delta -8 够不到断崖阈值 -20。
# 这正是生产判 CRASH、回测判 RISK_OFF 的那一类日子。
CRASH_MAIN_PCT = -3.05
CRASH_SMALL_PCT = -7.15
# 键名必须是 ratio_pct / delta_pct：analyzer 读的是这两个，写成 ratio / delta
# 不会报错,只是被当成缺值忽略,广度判据静默失效。
BREADTH_NO_CLIFF = {"ratio_pct": 28.0, "prev_ratio_pct": 36.0, "delta_pct": -8.0, "sample_size": 4000}
BREADTH_CLIFF = {"ratio_pct": 8.0, "prev_ratio_pct": 38.98, "delta_pct": -30.98, "sample_size": 4000}
MONEY_FLOW_RETREAT = {"trend": "retreat", "score": -25.0, "up_down_amount_ratio": 0.40, "sample_size": 4000}
AMOUNT_DISTRIBUTION_THIN = {"state": "thin", "sample_size": 4000}


def _index_frame(last_pct: float, rows: int = 260, base: float = 100.0) -> pd.DataFrame:
    """构造指数历史。

    rows 必须 >= 200：MA200 缺失时算出来的是 NaN，而 _structural_regime 用
    `value is None` 判空，NaN 会穿过去，BEAR 永不成立、全部塌成 NEUTRAL。
    """
    closes = [base] * (rows - 1) + [base * (1 + last_pct / 100.0)]
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=rows, freq="B").date,
            "close": closes,
            "open": closes,
            "high": closes,
            "low": closes,
            "pct_chg": [0.0] * (rows - 1) + [last_pct],
            "volume": [1e6] * rows,
        }
    )


def _day_df_map() -> dict[str, pd.DataFrame]:
    rows = 30
    return {
        "000001": pd.DataFrame(
            {
                "date": pd.date_range("2026-06-01", periods=rows, freq="B").date,
                "close": [10.0] * rows,
                "open": [10.0] * rows,
                "high": [10.2] * rows,
                "low": [9.8] * rows,
                "pct_chg": [0.0] * rows,
                "volume": [1e6] * rows,
                "amount": [1e8] * rows,
            }
        )
    }


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


class TestCrashSurvivesWithMoneyFlow:
    """行为回归：跑真 analyzer,不是 spy。"""

    def test_crash_day_reproduces_as_crash(self):
        """给了资金撤退这条确认判据,CRASH 就是 CRASH。"""
        out = _analyze_market_regime(
            _index_frame(CRASH_MAIN_PCT),
            FunnelConfig(),
            BREADTH_NO_CLIFF,
            _config(market_regime_analyzer=analyze_benchmark_and_tune_cfg),
            _index_frame(CRASH_SMALL_PCT),
            money_flow=MONEY_FLOW_RETREAT,
            amount_distribution=AMOUNT_DISTRIBUTION_THIN,
        )

        assert out["regime"] == "CRASH"

    def test_same_day_degrades_to_risk_off_without_money_flow(self):
        """对照:这就是修复前回测里发生的事——同一天掉到 RISK_OFF。

        断言 RISK_OFF 而非 CRASH,是为了让这个用例在 bug 回归时变红。
        """
        out = _analyze_market_regime(
            _index_frame(CRASH_MAIN_PCT),
            FunnelConfig(),
            BREADTH_NO_CLIFF,
            _config(market_regime_analyzer=analyze_benchmark_and_tune_cfg),
            _index_frame(CRASH_SMALL_PCT),
        )

        assert out["regime"] == "RISK_OFF"

    def test_crash_reasons_include_price_and_confirmation(self):
        """CRASH 的 panic_reasons 必须同时含价格判据和确认判据。"""
        out = _analyze_market_regime(
            _index_frame(CRASH_MAIN_PCT),
            FunnelConfig(),
            BREADTH_NO_CLIFF,
            _config(market_regime_analyzer=analyze_benchmark_and_tune_cfg),
            _index_frame(CRASH_SMALL_PCT),
            money_flow=MONEY_FLOW_RETREAT,
            amount_distribution=AMOUNT_DISTRIBUTION_THIN,
        )
        reasons = out.get("panic_reasons") or []

        assert any(reason.startswith("main_day_drop") for reason in reasons)
        assert any(reason.startswith("money_flow_retreat") for reason in reasons)

    def test_risk_off_fallthrough_reports_no_reasons(self):
        """记录这个坑:降级那条路返回空 panic_reasons,不代表价格判据没触发。"""
        out = _analyze_market_regime(
            _index_frame(CRASH_MAIN_PCT),
            FunnelConfig(),
            BREADTH_NO_CLIFF,
            _config(market_regime_analyzer=analyze_benchmark_and_tune_cfg),
            _index_frame(CRASH_SMALL_PCT),
        )

        assert out["regime"] == "RISK_OFF"
        assert not (out.get("panic_reasons") or [])

    def test_breadth_cliff_alone_still_confirms(self):
        """广度自己够到断崖阈值时,不依赖资金流也应判 CRASH。

        对应实测中唯一能复现的那天(2026-08-19,delta=-30.98)。
        """
        out = _analyze_market_regime(
            _index_frame(CRASH_MAIN_PCT),
            FunnelConfig(),
            BREADTH_CLIFF,
            _config(market_regime_analyzer=analyze_benchmark_and_tune_cfg),
            _index_frame(CRASH_SMALL_PCT),
        )

        assert out["regime"] == "CRASH"


class TestAnalyzerReceivesLiquidityContext:
    def test_money_flow_is_forwarded(self):
        seen: dict[str, object] = {}

        def spy(bench, smallcap, cfg, **kwargs):
            seen["money_flow"] = kwargs.get("money_flow")
            return {"regime": "NEUTRAL"}

        _analyze_market_regime(
            _index_frame(-1.5),
            FunnelConfig(),
            {},
            _config(market_regime_analyzer=spy),
            None,
            money_flow=MONEY_FLOW_RETREAT,
        )

        assert seen["money_flow"] == MONEY_FLOW_RETREAT

    def test_amount_distribution_is_forwarded(self):
        seen: dict[str, object] = {}

        def spy(bench, smallcap, cfg, **kwargs):
            seen["amount_distribution"] = kwargs.get("amount_distribution")
            return {"regime": "NEUTRAL"}

        _analyze_market_regime(
            _index_frame(-1.5),
            FunnelConfig(),
            {},
            _config(market_regime_analyzer=spy),
            None,
            amount_distribution=AMOUNT_DISTRIBUTION_THIN,
        )

        assert seen["amount_distribution"] == AMOUNT_DISTRIBUTION_THIN

    def test_breadth_and_smallcap_still_passed(self):
        """新增两个入参不得挤掉原有的两个。"""
        seen: dict[str, object] = {}

        def spy(bench, smallcap, cfg, **kwargs):
            seen["breadth"] = kwargs.get("breadth")
            seen["smallcap"] = smallcap
            return {"regime": "NEUTRAL"}

        _analyze_market_regime(
            _index_frame(0.0),
            FunnelConfig(),
            {"ratio_pct": 12.0},
            _config(market_regime_analyzer=spy),
            _index_frame(-3.0),
            money_flow=MONEY_FLOW_RETREAT,
            amount_distribution=AMOUNT_DISTRIBUTION_THIN,
        )

        assert seen["breadth"] == {"ratio_pct": 12.0}
        assert seen["smallcap"] is not None

    def test_defaults_to_none_when_omitted(self):
        """省略参数时保持向后兼容,走 market_regime 里的空字典兜底。"""
        seen: dict[str, object] = {}

        def spy(bench, smallcap, cfg, **kwargs):
            seen["money_flow"] = kwargs.get("money_flow")
            seen["amount_distribution"] = kwargs.get("amount_distribution")
            return {"regime": "NEUTRAL"}

        _analyze_market_regime(_index_frame(0.0), FunnelConfig(), {}, _config(market_regime_analyzer=spy))

        assert seen["money_flow"] is None
        assert seen["amount_distribution"] is None


class TestCalculatorInjection:
    def test_money_flow_calculator_receives_day_data_and_breadth(self):
        seen: dict[str, object] = {}

        def spy(df_map, breadth):
            seen["codes"] = list(df_map.keys())
            seen["breadth"] = breadth
            return MONEY_FLOW_RETREAT

        out = _calculate_market_money_flow(
            _day_df_map(),
            BREADTH_NO_CLIFF,
            _config(market_money_flow_calculator=spy),
        )

        assert seen["codes"] == ["000001"]
        assert seen["breadth"] == BREADTH_NO_CLIFF
        assert out == MONEY_FLOW_RETREAT

    def test_amount_distribution_calculator_receives_cfg_thresholds(self):
        """成交额分布要读当日 cfg 的门槛和窗口,不能用默认值。"""
        seen: dict[str, object] = {}

        def spy(df_map, min_avg_amount_wan, lookback):
            seen["codes"] = list(df_map.keys())
            seen["min_avg_amount_wan"] = min_avg_amount_wan
            seen["lookback"] = lookback
            return AMOUNT_DISTRIBUTION_THIN

        cfg = FunnelConfig()
        out = _calculate_amount_distribution(
            _day_df_map(),
            cfg,
            _config(amount_distribution_calculator=spy),
        )

        assert seen["codes"] == ["000001"]
        assert seen["min_avg_amount_wan"] == cfg.min_avg_amount_wan
        assert seen["lookback"] == cfg.amount_avg_window
        assert out == AMOUNT_DISTRIBUTION_THIN

    def test_missing_calculators_return_none(self):
        """没注入就返回 None,让 analyzer 走自己的兜底,保持旧行为。"""
        config = _config()

        assert _calculate_market_money_flow(_day_df_map(), BREADTH_NO_CLIFF, config) is None
        assert _calculate_amount_distribution(_day_df_map(), FunnelConfig(), config) is None


class TestConfigFields:
    def test_replay_config_carries_calculators(self):
        config = _config(
            market_money_flow_calculator=lambda *_a, **_k: {},
            amount_distribution_calculator=lambda *_a, **_k: {},
        )

        assert config.market_money_flow_calculator is not None
        assert config.amount_distribution_calculator is not None

    def test_replay_config_defaults_are_none(self):
        config = _config()

        assert config.market_money_flow_calculator is None
        assert config.amount_distribution_calculator is None
