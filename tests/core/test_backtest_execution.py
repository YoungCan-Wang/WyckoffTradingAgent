from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from core.backtest_execution import (
    ExitSimulationConfig,
    TradeRecord,
    build_daily_nav,
    calc_portfolio_metrics,
    calc_prior_momentum_pct,
    resolve_trade_exit,
)


def test_build_daily_nav_uses_single_return_chain() -> None:
    d1 = date(2026, 1, 5)
    d2 = date(2026, 1, 6)
    d3 = date(2026, 1, 7)
    record = TradeRecord(
        signal_date=d1,
        entry_date=d1,
        exit_date=d3,
        code="000001",
        name="平安银行",
        trigger="sos",
        score=1.0,
        entry_close=10.0,
        exit_close=12.0,
        ret_pct=20.0,
    )
    ohlc_cache = {"000001": {d1: (10, 10, 10, 10), d2: (10, 11, 10, 11), d3: (11, 12, 11, 12)}}

    nav = build_daily_nav([record], ohlc_cache, [d1, d2, d3], d1, d3)

    assert nav["positions_count"].tolist() == [1, 1, 1]
    assert nav["daily_ret_pct"].tolist() == pytest.approx([0.0, 10.0, 100 * (12 / 11 - 1)])
    assert nav["nav"].iloc[-1] == pytest.approx(1 + 0.1 + (12 / 11 - 1))


def test_calc_portfolio_metrics_empty_nav_has_stable_keys() -> None:
    metrics = calc_portfolio_metrics(build_daily_nav([], {}, [], date(2026, 1, 1), date(2026, 1, 2)))

    assert metrics["portfolio_trading_days"] == 0
    assert metrics["portfolio_avg_positions"] == 0.0
    assert metrics["portfolio_sharpe"] is None


def test_resolve_trade_exit_sltp_uses_threshold_price() -> None:
    d1 = date(2026, 1, 5)
    d2 = date(2026, 1, 6)
    full_df = _daily_close_frame([(d1, 10.0), (d2, 11.0)])
    day_ohlc = {d1: (10.0, 10.0, 10.0, 10.0), d2: (10.0, 11.8, 9.8, 11.0)}

    exit_close, exit_date, reason = resolve_trade_exit(
        full_df=full_df,
        day_ohlc=day_ohlc,
        trade_dates=[d1, d2],
        actual_entry_idx=0,
        actual_exit_idx=1,
        actual_exit_anchor=d2,
        signal_date=d1,
        entry_close=10.0,
        config=_exit_config(take_profit_pct=18.0),
    )

    assert exit_close == pytest.approx(11.8)
    assert exit_date == d2
    assert reason == "take_profit"


def test_resolve_trade_exit_sltp_zero_risk_controls_waits_for_time_exit() -> None:
    d1 = date(2026, 1, 5)
    d2 = date(2026, 1, 6)
    full_df = _daily_close_frame([(d1, 10.0), (d2, 12.0)])
    day_ohlc = {d1: (10.0, 10.0, 10.0, 10.0), d2: (10.0, 30.0, 1.0, 12.0)}

    exit_close, exit_date, reason = resolve_trade_exit(
        full_df=full_df,
        day_ohlc=day_ohlc,
        trade_dates=[d1, d2],
        actual_entry_idx=0,
        actual_exit_idx=1,
        actual_exit_anchor=d2,
        signal_date=d1,
        entry_close=10.0,
        config=_exit_config(stop_loss_pct=0.0, take_profit_pct=0.0, trailing_stop_pct=0.0),
    )

    assert exit_close == pytest.approx(12.0)
    assert exit_date == d2
    assert reason == "time_exit"


def test_time_exit_rolls_forward_when_anchor_day_is_locked_at_limit_down() -> None:
    d1, d2, d3 = (date(2026, 1, day) for day in (5, 6, 7))
    full_df = _daily_ohlc_frame(
        [
            (d1, 10.0, 10.0, 10.0, 10.0),
            (d2, 9.0, 9.0, 9.0, 9.0),
            (d3, 8.6, 9.2, 8.5, 9.1),
        ]
    )

    exit_close, exit_date, reason = resolve_trade_exit(
        full_df=full_df,
        day_ohlc={},
        trade_dates=[d1, d2, d3],
        actual_entry_idx=0,
        actual_exit_idx=1,
        actual_exit_anchor=d2,
        signal_date=d1,
        entry_close=10.0,
        config=_exit_config(exit_mode="close_only"),
        code="000001",
    )

    assert exit_close == pytest.approx(9.1)
    assert exit_date == d3
    assert reason == "time_exit"


def test_stop_loss_skips_limit_down_locked_day_and_fills_next_session() -> None:
    d1, d2, d3 = (date(2026, 1, day) for day in (5, 6, 7))
    full_df = _daily_ohlc_frame(
        [
            (d1, 10.0, 10.0, 10.0, 10.0),
            (d2, 9.0, 9.0, 9.0, 9.0),
            (d3, 8.6, 9.2, 8.5, 9.1),
        ]
    )
    day_ohlc = {
        d1: (10.0, 10.0, 10.0, 10.0),
        d2: (9.0, 9.0, 9.0, 9.0),
        d3: (8.6, 9.2, 8.5, 9.1),
    }

    exit_close, exit_date, reason = resolve_trade_exit(
        full_df=full_df,
        day_ohlc=day_ohlc,
        trade_dates=[d1, d2, d3],
        actual_entry_idx=0,
        actual_exit_idx=2,
        actual_exit_anchor=d3,
        signal_date=d1,
        entry_close=10.0,
        config=_exit_config(stop_loss_pct=-7.0),
        code="000001",
    )

    assert exit_close == pytest.approx(8.6)
    assert exit_date == d3
    assert reason == "stop_loss"


def test_stop_loss_uses_entry_day_close_not_entry_price_for_limit_down() -> None:
    """开盘买入后收阳，次日相对前收一字跌停：不得用入场开盘价漏判锁死。"""
    d1, d2, d3 = (date(2026, 1, day) for day in (5, 6, 7))
    full_df = _daily_ohlc_frame(
        [
            (d1, 10.0, 10.5, 9.9, 10.2),
            (d2, 9.18, 9.18, 9.18, 9.18),
            (d3, 8.6, 9.0, 8.5, 8.8),
        ]
    )
    day_ohlc = {
        d1: (10.0, 10.5, 9.9, 10.2),
        d2: (9.18, 9.18, 9.18, 9.18),
        d3: (8.6, 9.0, 8.5, 8.8),
    }

    exit_close, exit_date, reason = resolve_trade_exit(
        full_df=full_df,
        day_ohlc=day_ohlc,
        trade_dates=[d1, d2, d3],
        actual_entry_idx=0,
        actual_exit_idx=2,
        actual_exit_anchor=d3,
        signal_date=d1,
        entry_close=10.0,
        config=_exit_config(stop_loss_pct=-7.0),
        code="000001",
    )

    assert exit_close == pytest.approx(8.6)
    assert exit_date == d3
    assert reason == "stop_loss"


def test_time_exit_keeps_scanning_beyond_five_locked_limit_down_days() -> None:
    days = [date(2026, 1, day) for day in range(5, 13)]
    rows = [(days[0], 10.0, 10.0, 10.0, 10.0)]
    close = 10.0
    for day in days[1:-1]:
        close = round(close * 0.9, 4)
        rows.append((day, close, close, close, close))
    rows.append((days[-1], 4.8, 5.2, 4.7, 5.0))
    full_df = _daily_ohlc_frame(rows)

    exit_close, exit_date, reason = resolve_trade_exit(
        full_df=full_df,
        day_ohlc={},
        trade_dates=days,
        actual_entry_idx=0,
        actual_exit_idx=1,
        actual_exit_anchor=days[1],
        signal_date=days[0],
        entry_close=10.0,
        config=_exit_config(exit_mode="close_only"),
        code="000001",
    )

    assert exit_close == pytest.approx(5.0)
    assert exit_date == days[-1]
    assert reason == "time_exit"


def _exit_config(**overrides) -> ExitSimulationConfig:
    values = {
        "exit_mode": "sltp",
        "stop_loss_pct": -7.0,
        "take_profit_pct": 0.0,
        "trailing_stop_pct": 0.0,
        "trailing_activate_pct": 0.0,
        "sltp_priority": "stop_first",
        "atr_period": 14,
        "atr_multiplier": 2.0,
        "atr_hard_stop_pct": -9.0,
    }
    values.update(overrides)
    return ExitSimulationConfig(**values)


def _daily_close_frame(rows: list[tuple[date, float]]):
    return pd.DataFrame({"date": [row[0] for row in rows], "close": [row[1] for row in rows]})


def _daily_ohlc_frame(rows: list[tuple[date, float, float, float, float]]):
    return pd.DataFrame(
        {
            "date": [row[0] for row in rows],
            "open": [row[1] for row in rows],
            "high": [row[2] for row in rows],
            "low": [row[3] for row in rows],
            "close": [row[4] for row in rows],
        }
    )


def _ohlc_from_closes(days: list[date], closes: dict[date, float]) -> dict:
    return {d: (closes[d], closes[d], closes[d], closes[d]) for d in days}


def test_prior_momentum_matches_panel_definition_day_by_day() -> None:
    """回测标量口径与效果检验面板的向量化口径必须逐日相等。

    这条是整列的意义所在：同动量对照要求两组「用同一把尺子量」。两边各算一遍 20 日
    涨幅（含不含 T 日、shift 几格）而结果不同的话，不会报错，只会让对照组挑错邻居。
    """
    import pandas as pd_

    from core.funnel_effect_panels import MOM_LOOKBACK_BARS, build_panels, normalize_market_frame

    stamps = pd_.bdate_range("2026-01-01", periods=40)
    closes = [10.0 + i * 0.3 for i in range(40)]
    panels = build_panels(
        normalize_market_frame(
            pd_.DataFrame(
                {
                    "ts_code": ["000001.SZ"] * 40,
                    "trade_date": [d.strftime("%Y%m%d") for d in stamps],
                    "open": closes,
                    "close": closes,
                    "amount": [1e9] * 40,
                }
            )
        ),
        min_amount_wan=0.0,
    )
    days = [d.date() for d in stamps]
    day_ohlc = _ohlc_from_closes(days, dict(zip(days, closes, strict=True)))

    compared = 0
    for i in range(MOM_LOOKBACK_BARS, len(days)):
        panel_value = panels.mom20[stamps[i].strftime("%Y-%m-%d")]["000001"]
        assert calc_prior_momentum_pct(days, day_ohlc, days[i]) == pytest.approx(panel_value)
        compared += 1
    assert compared == len(days) - MOM_LOOKBACK_BARS


def test_prior_momentum_is_none_when_history_shorter_than_lookback() -> None:
    """历史不足 20 根返回 None，不能填 0.0。

    0.0 是「横盘」这个真实档位。用它填缺失会把样本头几天与新股混进零动量那一档，
    配对时按零动量找邻居，控制组就选错了人，而这种偏差不报错。
    """
    days = [d.date() for d in pd.bdate_range("2026-01-01", periods=25)]
    day_ohlc = _ohlc_from_closes(days, {d: 10.0 + i for i, d in enumerate(days)})

    assert calc_prior_momentum_pct(days, day_ohlc, days[19]) is None
    assert calc_prior_momentum_pct(days, day_ohlc, days[20]) is not None


def test_prior_momentum_counts_the_stocks_own_bars_not_the_market_calendar() -> None:
    """停牌票要按自己的交易日回看 20 根，不能按全市场日历数 20 格。

    按全市场日历数，停牌日会被算作有行情的日子，实际回看窗口被拉长、动量偏小。
    这里把停牌区放进回看窗口内，两种做法相差 5.9pct——只在停牌票上出现，均值上看不出来。
    """
    market_days = [d.date() for d in pd.bdate_range("2026-01-01", periods=45)]
    halted = set(market_days[30:35])
    own_days = [d for d in market_days if d not in halted]

    closes, price = {}, 10.0
    for day in own_days:
        closes[day] = price
        price *= 1.01
    day_ohlc = _ohlc_from_closes(own_days, closes)
    target = own_days[-1]

    actual = calc_prior_momentum_pct(own_days, day_ohlc, target)
    own_ref = own_days[own_days.index(target) - 20]
    market_ref = market_days[market_days.index(target) - 20]

    assert own_ref != market_ref, "构造无效：停牌区没落在回看窗口内，两种做法本来就同解"
    assert actual == pytest.approx(100.0 * (closes[target] / closes[own_ref] - 1.0))
    assert actual != pytest.approx(100.0 * (closes[target] / closes[market_ref] - 1.0))


def test_prior_momentum_rejects_dates_outside_the_series() -> None:
    """信号日不在这只票的行情里就返回 None——不能顺移到最近的一天。

    顺移会让动量的基准日与信号日错开，且错开多少取决于停牌长度，静默不可查。
    """
    days = [d.date() for d in pd.bdate_range("2026-01-01", periods=30)]
    day_ohlc = _ohlc_from_closes(days, {d: 10.0 + i for i, d in enumerate(days)})
    # 序列里的空档（周六）：必须落在 20 根之后，否则「不足回看」会先命中，用例就废了
    gap = next(d + timedelta(days=1) for d in days[22:] if (d + timedelta(days=1)) not in day_ohlc)

    assert gap not in day_ohlc and days[0] < gap < days[-1]
    assert calc_prior_momentum_pct(days, day_ohlc, gap) is None
    assert calc_prior_momentum_pct(days, day_ohlc, date(2030, 1, 1)) is None


def test_prior_momentum_guards_non_positive_and_non_finite_base() -> None:
    """基准价 <=0 或非有限值返回 None,不能算出 inf 混进配对。"""
    from core.funnel_effect_panels import momentum_pct

    assert momentum_pct(10.0, 0.0) is None
    assert momentum_pct(10.0, -1.0) is None
    assert momentum_pct(None, 5.0) is None
    assert momentum_pct(10.0, None) is None
    assert momentum_pct(float("nan"), 5.0) is None
    assert momentum_pct(11.0, 10.0) == pytest.approx(10.0)


def test_trade_record_carries_prior_momentum_into_csv_columns() -> None:
    """列要落进 trades_*.csv;缺值写空串而不是 0。"""
    record = TradeRecord(
        signal_date=date(2026, 3, 2),
        entry_date=date(2026, 3, 3),
        exit_date=date(2026, 3, 10),
        code="000001",
        name="平安银行",
        trigger="sos",
        score=1.0,
        entry_close=10.0,
        exit_close=11.0,
        ret_pct=10.0,
        prior_mom20_pct=12.5,
    )
    frame = pd.DataFrame([record.__dict__, TradeRecord(**{**record.__dict__, "prior_mom20_pct": None}).__dict__])

    assert "prior_mom20_pct" in frame.columns
    assert frame["prior_mom20_pct"].tolist()[0] == pytest.approx(12.5)
    assert frame.to_csv(index=False).splitlines()[2].split(",")[-1] == ""
