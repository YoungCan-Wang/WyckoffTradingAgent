from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from core.tail_buy.reporting import build_tail_buy_markdown
from core.tail_buy.strategy import (
    DECISION_BUY,
    DECISION_SKIP,
    DECISION_WATCH,
    DEFAULT_TAIL_BUY_STRATEGY_CONFIG,
    TailBuyCandidate,
    TailBuyStrategyConfig,
    _normalize_signal_score,
    apply_policy_weight_adjustments,
    board_scaled_strategy_config,
    build_5m_summary,
    build_llm_prompt,
    compute_tail_features,
    evaluate_rule_decision,
    merge_rule_and_llm,
    pick_tail_candidates,
    select_llm_overlay_candidates,
)
from workflows.tail_buy_config import tail_buy_strategy_config_from_env
from workflows.tail_buy_rule_scan import run_rule_scan_batch
from workflows.tail_buy_utils import TZ


def _make_intraday_df(
    *,
    start: float,
    end: float,
    bars: int = 180,
    tail_boost: float = 0.0,
    tail_volume_mult: float = 1.0,
) -> pd.DataFrame:
    idx = pd.date_range(
        start=datetime(2026, 4, 21, 9, 30),
        periods=bars,
        freq="1min",
        tz="Asia/Shanghai",
    )
    base = pd.Series([start + (end - start) * i / max(bars - 1, 1) for i in range(bars)])
    if tail_boost != 0.0:
        tail_n = min(30, bars)
        tail_delta = pd.Series(
            [tail_boost * (i + 1) / tail_n for i in range(tail_n)],
            index=base.index[-tail_n:],
        )
        base.iloc[-tail_n:] = base.iloc[-tail_n:].to_numpy() + tail_delta.to_numpy()
    close = base
    open_ = close.shift(1).fillna(close.iloc[0]) * 0.999
    high = close * 1.003
    low = close * 0.997
    volume = pd.Series([1200.0] * bars)
    tail_n = min(30, bars)
    volume.iloc[-tail_n:] = volume.iloc[-tail_n:] * tail_volume_mult
    amount = close * volume
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": open_.values,
            "high": high.values,
            "low": low.values,
            "close": close.values,
            "volume": volume.values,
            "amount": amount.values,
        }
    )


def _make_daily_history_with_prev_close(prev_close: float) -> pd.DataFrame:
    """构造仅用于提供昨收的日K：倒数第二行=昨天收盘价，供涨停判断使用。"""
    dates = pd.date_range(start=datetime(2026, 3, 1), periods=21, freq="D")
    close = pd.Series([prev_close] * 21, dtype="float64")
    open_ = close * 0.999
    high = close * 1.001
    low = close * 0.999
    volume = pd.Series([1000.0] * 21)
    return pd.DataFrame(
        {
            "date": dates,
            "open": open_.values,
            "high": high.values,
            "low": low.values,
            "close": close.values,
            "volume": volume.values,
        }
    )


def _make_daily_trap_df() -> pd.DataFrame:
    dates = pd.date_range(start=datetime(2026, 3, 20), periods=25, freq="D")
    close = pd.Series([8.0 + i * 0.04 for i in range(25)], dtype="float64")
    open_ = close * 0.998
    high = close * 1.006
    low = close * 0.994
    volume = pd.Series([1000.0] * 25)
    open_.iloc[-1] = 11.8
    high.iloc[-1] = 12.4
    low.iloc[-1] = 10.7
    close.iloc[-1] = 11.2
    volume.iloc[-1] = 2600.0
    return pd.DataFrame(
        {
            "date": dates,
            "open": open_.values,
            "high": high.values,
            "low": low.values,
            "close": close.values,
            "volume": volume.values,
        }
    )


def _make_atr_daily_history(atr_value: float, close_base: float = 10.0, periods: int = 20) -> pd.DataFrame:
    """构造 close 恒定、每日 high-low=atr_value 的日K，使 calc_atr(df, 14) 精确等于 atr_value。"""
    dates = pd.date_range(start=datetime(2026, 3, 1), periods=periods, freq="D")
    close = pd.Series([close_base] * periods, dtype="float64")
    half = atr_value / 2.0
    high = close + half
    low = close - half
    volume = pd.Series([1000.0] * periods)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close.values,
            "high": high.values,
            "low": low.values,
            "close": close.values,
            "volume": volume.values,
        }
    )


def test_pick_tail_candidates_filters_prev_trade_day_and_status():
    rows = [
        {
            "code": 301090,
            "name": "华润材料",
            "signal_type": "spring",
            "signal_score": 3.2,
            "status": "pending",
            "signal_date": "2026-04-20",
        },
        {
            "code": 301090,
            "name": "华润材料",
            "signal_type": "spring",
            "signal_score": 2.9,
            "status": "confirmed",
            "signal_date": "2026-04-20",
            "regime": "RISK_ON",
            "snap_support": 9.8,
        },
        {
            "code": "600000",
            "name": "浦发银行",
            "signal_type": "sos",
            "signal_score": 1.5,
            "status": "expired",
            "signal_date": "2026-04-20",
        },
        {
            "code": "000001",
            "name": "平安银行",
            "signal_type": "lps",
            "signal_score": 2.1,
            "status": "pending",
            "signal_date": "2026-04-18",
        },
        {
            "code": "002217",
            "name": "合力泰",
            "signal_type": "sos",
            "signal_score": 5.1,
            "status": "pending",
            "signal_date": "2026-04-20",
        },
    ]
    got = pick_tail_candidates(rows, cutoff_date="2026-04-20")
    assert [x.code for x in got] == ["301090", "002217"]
    assert got[0].status == "confirmed"
    assert got[0].market_regime == "RISK_ON"
    assert got[0].snap["snap_support"] == 9.8


def test_pick_tail_candidates_prefers_newer_pending_over_stale_confirmed():
    rows = [
        {
            "code": "603661",
            "name": "恒林股份",
            "signal_type": "lps",
            "signal_score": 0.44,
            "status": "confirmed",
            "signal_date": "2026-06-10",
            "snap_support": 32.18,
        },
        {
            "code": "603661",
            "name": "恒林股份",
            "signal_type": "sos",
            "signal_score": 4.08,
            "status": "pending",
            "signal_date": "2026-06-26",
            "snap_support": 37.50,
        },
    ]

    got = pick_tail_candidates(rows, cutoff_date="2026-06-01")

    assert len(got) == 1
    assert got[0].signal_date == "2026-06-26"
    assert got[0].status == "pending"
    assert got[0].snap["snap_support"] == 37.50


def test_apply_policy_weight_adjustments_downgrades_weak_signal_buy():
    candidate = TailBuyCandidate(
        code="000001",
        name="弱信号",
        signal_date="2026-07-01",
        status="confirmed",
        signal_type="lps",
        signal_score=80.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=80.0,
    )

    result = apply_policy_weight_adjustments(
        [candidate],
        {"lps": 0.5},
        policy_meta={
            "source": "远端",
            "report_date": "2026-07-04",
            "horizon": "5",
            "age_days": 0,
            "execution_policy": "shadow",
            "execution_scope": "tail_buy_and_funnel_shadow",
            "active_scope": "尾盘+漏斗shadow",
            "next_action": "manual_review_dynamic_on",
            "formal_dynamic_allowed": True,
            "backtest_confirmation_text": "待复核(need backtest)",
            "promotion_checklist_summary": "样本=通过；回测=待复核",
            "tail_buy_weights_active": True,
            "funnel_shadow_weights_active": True,
            "funnel_formal_weights_active": False,
        },
    )

    assert result[0].rule_decision == DECISION_WATCH
    assert result[0].final_decision == DECISION_WATCH
    assert result[0].rule_score == 40.0
    assert result[0].features["policy_weight_multiplier"] == 0.5
    assert result[0].features["policy_weight_old_score"] == 80.0
    assert result[0].features["policy_weight_new_score"] == 40.0
    assert result[0].features["policy_weight_source"] == "远端"
    assert result[0].features["policy_weight_report_date"] == "2026-07-04"
    assert result[0].features["policy_weight_horizon"] == "5"
    assert result[0].features["policy_weight_execution_policy"] == "shadow"
    assert result[0].features["policy_weight_execution_policy_label"] == "shadow 对照(shadow)"
    assert result[0].features["policy_weight_execution_scope"] == "tail_buy_and_funnel_shadow"
    assert result[0].features["policy_weight_active_scope"] == "尾盘+漏斗shadow"
    assert result[0].features["policy_weight_next_action"] == "manual_review_dynamic_on"
    assert result[0].features["policy_weight_next_action_label"] == "进入人工晋级评审（非正式生效）"
    assert result[0].features["policy_weight_formal_dynamic_allowed"] is True
    assert result[0].features["policy_weight_formal_dynamic_label"] == "允许正式生效"
    assert result[0].features["policy_weight_tail_buy_weights_active"] is True
    assert result[0].features["policy_weight_funnel_shadow_weights_active"] is True
    assert result[0].features["policy_weight_funnel_formal_weights_active"] is False
    assert "归因治理调权(lps) x0.50: 80.0->40.0" in result[0].rule_reasons
    assert "调权后低于买入线，尾盘只观察" in result[0].rule_reasons


def test_apply_policy_weight_adjustments_uses_scoped_signal_weight():
    candidate = TailBuyCandidate(
        code="000003",
        name="分桶弱信号",
        signal_date="2026-07-01",
        status="confirmed",
        signal_type="lps",
        signal_score=80.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=80.0,
        market_regime="RISK_ON",
        candidate_lane="trend_pullback",
        entry_type="wyckoff_structure",
    )

    result = apply_policy_weight_adjustments(
        [candidate],
        {"lps|regime=RISK_ON|lane=trend_pullback|entry=wyckoff_structure": 0.5},
    )

    assert result[0].rule_decision == DECISION_WATCH
    assert result[0].final_decision == DECISION_WATCH
    assert result[0].rule_score == 40.0
    assert result[0].features["policy_weight_multiplier"] == 0.5


def test_apply_policy_weight_adjustments_upweight_does_not_auto_buy():
    candidate = TailBuyCandidate(
        code="000002",
        name="强信号",
        signal_date="2026-07-01",
        status="confirmed",
        signal_type="sos",
        signal_score=80.0,
        rule_score=65.0,
        rule_decision=DECISION_WATCH,
        final_decision=DECISION_WATCH,
        priority_score=65.0,
    )

    result = apply_policy_weight_adjustments([candidate], {"sos": 1.15})

    assert result[0].rule_decision == DECISION_WATCH
    assert result[0].final_decision == DECISION_WATCH
    assert result[0].rule_score == 74.75
    assert result[0].features["policy_weight_multiplier"] == 1.15
    assert "归因治理调权(sos) x1.15: 65.0->74.8" in result[0].rule_reasons


def test_evaluate_rule_decision_buy_and_skip_split():
    strong = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        snap={"snap_support": 9.8},
    )
    weak = TailBuyCandidate(
        code="600000",
        name="浦发银行",
        signal_date="2026-04-20",
        status="pending",
        signal_type="sos",
        signal_score=1.0,
    )
    strong_df = _make_intraday_df(start=10.0, end=10.9, tail_boost=0.8, tail_volume_mult=2.0)
    weak_df = _make_intraday_df(start=10.0, end=9.6, tail_boost=-0.2, tail_volume_mult=0.6)

    strong_out = evaluate_rule_decision(strong, strong_df, style="hybrid")
    weak_out = evaluate_rule_decision(weak, weak_df, style="hybrid")

    assert strong_out.rule_decision in {DECISION_BUY, DECISION_WATCH}
    assert strong_out.rule_score > weak_out.rule_score
    assert weak_out.rule_decision == DECISION_SKIP


def test_tail_buy_scores_latest_intraday_session_only():
    old_day = _make_intraday_df(start=5.0, end=5.2, bars=80)
    old_day["datetime"] = pd.date_range(datetime(2026, 4, 20, 9, 30), periods=len(old_day), freq="1min", tz=TZ)
    new_day = _make_intraday_df(start=10.0, end=10.8, bars=120, tail_boost=0.6, tail_volume_mult=1.8)
    new_day["datetime"] = pd.date_range(datetime(2026, 4, 21, 9, 30), periods=len(new_day), freq="1min", tz=TZ)
    df = pd.concat([old_day, new_day], ignore_index=True)

    features = compute_tail_features(df)

    assert features["bars"] == len(new_day)
    assert abs(features["first_open"] - new_day["open"].iloc[0]) < 0.01
    assert features["day_low"] >= 9.9


def test_tail_buy_5m_summary_uses_latest_intraday_session_only():
    old_day = _make_intraday_df(start=5.0, end=5.2, bars=80)
    old_day["datetime"] = pd.date_range(datetime(2026, 4, 20, 9, 30), periods=len(old_day), freq="1min", tz=TZ)
    new_day = _make_intraday_df(start=10.0, end=10.8, bars=120, tail_boost=0.6, tail_volume_mult=1.8)
    new_day["datetime"] = pd.date_range(datetime(2026, 4, 21, 9, 30), periods=len(new_day), freq="1min", tz=TZ)
    df = pd.concat([old_day, new_day], ignore_index=True)

    summary = build_5m_summary(df)

    assert "2026-04-20" not in summary
    assert "O10." in summary
    assert "C11." in summary
    assert "O5." not in summary
    assert "C5." not in summary


def test_old_confirmed_signal_can_buy_when_current_tail_confirms():
    candidate = TailBuyCandidate(
        code="000920",
        name="沃顿科技",
        signal_date="2026-04-01",
        status="confirmed",
        signal_type="evr",
        signal_score=4.0,
        snap={"snap_support": 9.8},
    )
    df = _make_intraday_df(start=10.0, end=10.8, tail_boost=0.8, tail_volume_mult=2.0)
    df["datetime"] = pd.date_range(datetime(2026, 4, 21, 9, 30), periods=len(df), freq="1min", tz="Asia/Shanghai")

    out = evaluate_rule_decision(
        candidate,
        df,
        style="hybrid",
        config=TailBuyStrategyConfig(
            chase_day_ret_pct=30.0,
            chase_high_ret_pct=30.0,
            naked_support_extension_pct=30.0,
        ),
    )

    assert out.rule_decision == DECISION_BUY
    assert out.features["signal_age_days"] == 20
    assert "信号已超过" not in "；".join(out.rule_reasons)


def test_intraday_chase_is_watch_not_buy():
    candidate = TailBuyCandidate(
        code="600620",
        name="沪杭甬",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="evr",
        signal_score=3.5,
        snap={"snap_support": 9.8},
    )
    df = _make_intraday_df(start=10.0, end=11.4, tail_boost=0.1, tail_volume_mult=1.3)

    out = evaluate_rule_decision(candidate, df, style="trend")

    assert out.rule_decision == DECISION_WATCH
    assert "日内涨幅过大" in "；".join(out.rule_reasons)


def test_intraday_chase_threshold_relaxed_for_chinext_board():
    """同样14%日内涨幅，创业板/科创板的追高门槛应比主板更宽松，不应被误杀。"""
    candidate = TailBuyCandidate(
        code="300620",
        name="创业板测试股",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="evr",
        signal_score=3.5,
        snap={"snap_support": 9.8},
    )
    df = _make_intraday_df(start=10.0, end=11.4, tail_boost=0.1, tail_volume_mult=1.3)

    out = evaluate_rule_decision(candidate, df, style="trend")

    assert "日内涨幅过大" not in "；".join(out.rule_reasons)


class TestBoardScaledStrategyConfig:
    def test_main_board_returns_unchanged_config(self):
        base = TailBuyStrategyConfig()
        scaled = board_scaled_strategy_config(base, "600001")
        assert scaled == base

    def test_bse_board_returns_unchanged_config(self):
        base = TailBuyStrategyConfig()
        scaled = board_scaled_strategy_config(base, "430001")
        assert scaled == base

    def test_chinext_board_doubles_ret_pct_fields(self):
        base = TailBuyStrategyConfig()
        scaled = board_scaled_strategy_config(base, "300620")
        assert scaled.chase_day_ret_pct == base.chase_day_ret_pct * 2.0
        assert scaled.chase_high_ret_pct == base.chase_high_ret_pct * 2.0
        assert scaled.blowoff_high_ret_pct == base.blowoff_high_ret_pct * 2.0
        assert scaled.naked_support_extension_pct == base.naked_support_extension_pct * 2.0
        assert scaled.daily_trap_ma20_extension_pct == base.daily_trap_ma20_extension_pct * 2.0

    def test_star_board_doubles_ret_pct_fields(self):
        base = TailBuyStrategyConfig()
        scaled = board_scaled_strategy_config(base, "688620")
        assert scaled.chase_day_ret_pct == base.chase_day_ret_pct * 2.0

    def test_unrelated_fields_stay_unchanged(self):
        base = TailBuyStrategyConfig()
        scaled = board_scaled_strategy_config(base, "300620")
        assert scaled.weak_naked_day_ret_pct == base.weak_naked_day_ret_pct
        assert scaled.weak_naked_tail30_ret_pct == base.weak_naked_tail30_ret_pct
        assert scaled.blowoff_tail_volume_share == base.blowoff_tail_volume_share

    def test_none_config_defaults_before_scaling(self):
        scaled = board_scaled_strategy_config(None, "300620")
        assert scaled.chase_day_ret_pct == DEFAULT_TAIL_BUY_STRATEGY_CONFIG.chase_day_ret_pct * 2.0


def test_naked_evr_far_from_support_is_watch_not_buy():
    candidate = TailBuyCandidate(
        code="000920",
        name="沃顿科技",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="evr",
        signal_score=8.0,
        snap={"snap_support": 9.8},
    )
    df = _make_intraday_df(start=11.3, end=11.6, tail_boost=0.1, tail_volume_mult=1.4)

    out = evaluate_rule_decision(candidate, df, style="hybrid")

    assert out.rule_decision == DECISION_WATCH
    assert "远离确认支撑" in "；".join(out.rule_reasons)


def test_weak_naked_evr_is_watch_not_buy():
    candidate = TailBuyCandidate(
        code="603519",
        name="立霸股份",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="evr",
        signal_score=8.0,
        snap={"snap_support": 9.8},
    )
    df = _make_intraday_df(start=10.0, end=10.04, tail_boost=0.0, tail_volume_mult=1.0)

    out = evaluate_rule_decision(candidate, df, style="hybrid")

    assert out.rule_decision == DECISION_WATCH
    assert "裸SOS/EVR尾盘动能不足" in "；".join(out.rule_reasons)


def test_strong_intraday_trend_gets_hold_vwap_feature():
    candidate = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="sos",
        signal_score=6.0,
        snap={"snap_support": 9.8},
    )
    df = _make_intraday_df(start=10.0, end=10.9, tail_boost=0.2, tail_volume_mult=1.2)

    out = evaluate_rule_decision(candidate, df, style="trend")

    assert out.features["strong_hold_vwap"] is True
    assert out.features["hold_vwap_ratio"] >= 0.82
    assert "全天强势守VWAP" in "；".join(out.rule_reasons)


def test_pending_strong_tail_signal_stays_watch_by_default():
    candidate = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="pending",
        signal_type="sos",
        signal_score=6.0,
        snap={"snap_support": 9.8},
    )
    strong_df = _make_intraday_df(start=10.0, end=10.9, tail_boost=0.8, tail_volume_mult=2.0)

    out = evaluate_rule_decision(candidate, strong_df, style="hybrid")

    assert out.rule_score >= 72.0
    assert out.rule_decision == DECISION_WATCH
    assert out.final_decision == DECISION_WATCH
    assert "未二次确认" in "；".join(out.rule_reasons)


def test_pending_strong_tail_signal_can_buy_when_gate_disabled():
    candidate = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="pending",
        signal_type="sos",
        signal_score=6.0,
        snap={"snap_support": 9.8},
    )
    strong_df = _make_intraday_df(start=10.0, end=10.9, tail_boost=0.8, tail_volume_mult=2.0)

    out = evaluate_rule_decision(
        candidate,
        strong_df,
        style="hybrid",
        config=TailBuyStrategyConfig(
            confirmed_only_buy=False,
            chase_day_ret_pct=30.0,
            chase_high_ret_pct=30.0,
            naked_support_extension_pct=30.0,
        ),
    )

    assert out.rule_score >= 72.0
    assert out.rule_decision == DECISION_BUY
    assert "未二次确认" not in "；".join(out.rule_reasons)


def test_confirmed_tail_signal_skips_after_breaking_confirm_support():
    candidate = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        snap={"snap_support": 10.0},
    )
    df = _make_intraday_df(start=10.1, end=10.8, tail_boost=0.3, tail_volume_mult=1.5)
    df.loc[20, "low"] = 9.95

    out = evaluate_rule_decision(candidate, df, style="hybrid")

    assert out.rule_decision == DECISION_SKIP
    assert out.features["day_low_breached_support"] is True
    assert "跌破确认支撑" in "；".join(out.rule_reasons)


def test_tail_buy_skips_when_support_anchor_is_missing():
    candidate = TailBuyCandidate(
        code="603039",
        name="泛微网络",
        signal_date="2026-06-11",
        status="confirmed",
        signal_type="evr",
        signal_score=2.8,
        market_regime="NEUTRAL",
    )
    df = _make_intraday_df(start=46.0, end=49.2, tail_boost=0.8, tail_volume_mult=2.0)

    out = evaluate_rule_decision(candidate, df, style="hybrid")

    assert out.rule_decision == DECISION_SKIP
    assert "缺少确认支撑位" in "；".join(out.rule_reasons)


def test_tail_buy_blocks_defensive_regime_single_evr():
    candidate = TailBuyCandidate(
        code="300581",
        name="晨曦航空",
        signal_date="2026-06-12",
        status="confirmed",
        signal_type="evr",
        signal_score=2.7,
        market_regime="RISK_OFF",
        snap={"snap_support": 10.94},
    )
    df = _make_intraday_df(start=12.4, end=13.1, tail_boost=0.6, tail_volume_mult=2.0)

    out = evaluate_rule_decision(candidate, df, style="hybrid")

    assert out.rule_decision == DECISION_SKIP
    assert "RISK_OFF单EVR只观察" in "；".join(out.rule_reasons)


def test_tail_buy_blocks_intraday_repair_single_sos():
    candidate = TailBuyCandidate(
        code="300308",
        name="中际旭创",
        signal_date="2026-06-12",
        status="confirmed",
        signal_type="sos",
        signal_score=3.6,
        market_regime="PANIC_REPAIR_INTRADAY",
        snap={"snap_support": 88.0},
    )
    df = _make_intraday_df(start=90.0, end=94.0, tail_boost=0.8, tail_volume_mult=1.8)

    out = evaluate_rule_decision(candidate, df, style="hybrid")

    assert out.rule_decision == DECISION_SKIP
    assert "PANIC_REPAIR_INTRADAY单SOS只观察" in "；".join(out.rule_reasons)


def test_confirmed_tail_signal_skips_blowoff_reversal():
    candidate = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="sos",
        signal_score=6.0,
        snap={"snap_support": 9.5},
    )
    df = _make_intraday_df(start=10.0, end=10.4, tail_boost=-0.2, tail_volume_mult=8.0)
    df.loc[80, "high"] = 11.2
    df["amount"] = df["close"] * df["volume"]

    out = evaluate_rule_decision(candidate, df, style="hybrid")

    assert out.rule_decision == DECISION_SKIP
    assert out.features["tail_blowoff_reversal"] is True
    assert "冲高回落" in "；".join(out.rule_reasons)


def test_daily_trap_pressure_downgrades_tail_buy_to_watch():
    base = dict(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="sos",
        signal_score=6.0,
        snap={"snap_support": 9.8},
    )
    intraday = _make_intraday_df(start=10.3, end=11.2, tail_boost=0.8, tail_volume_mult=1.8)
    config = TailBuyStrategyConfig(
        daily_trap_gate_enabled=False,
        naked_support_extension_pct=30.0,
        chase_day_ret_pct=20.0,
        chase_high_ret_pct=25.0,
    )
    no_gate = evaluate_rule_decision(
        TailBuyCandidate(**base),
        intraday,
        style="trend",
        config=config,
    )
    gated = evaluate_rule_decision(
        TailBuyCandidate(**base),
        intraday,
        style="trend",
        config=TailBuyStrategyConfig(
            naked_support_extension_pct=30.0,
            chase_day_ret_pct=20.0,
            chase_high_ret_pct=25.0,
        ),
        daily_history=_make_daily_trap_df(),
    )

    assert no_gate.rule_decision == DECISION_BUY
    assert gated.rule_decision == DECISION_WATCH
    assert gated.features["daily_trap_pressure"] is True
    assert "日线" in "；".join(gated.rule_reasons)


def test_evaluate_rule_decision_flags_limit_up_touched():
    # 昨收10.0，主板涨停价=11.0；分钟线最高触及11.0，判定为触及涨停。
    intraday = _make_intraday_df(start=10.9, end=11.0)
    candidate = evaluate_rule_decision(
        TailBuyCandidate(
            code="603928",
            name="兴业股份",
            signal_date="2026-07-07",
            status="confirmed",
            signal_type="spring",
            signal_score=6.0,
            snap={"snap_support": 10.5},
        ),
        intraday,
        style="trend",
        daily_history=_make_daily_history_with_prev_close(10.0),
    )

    assert candidate.features["limit_up_touched"] is True


def test_evaluate_rule_decision_does_not_flag_normal_pullback():
    # 昨收10.0，涨幅约2.5%，远未触及涨停线。
    intraday = _make_intraday_df(start=9.9, end=10.25)
    candidate = evaluate_rule_decision(
        TailBuyCandidate(
            code="300649",
            name="杭州园林",
            signal_date="2026-07-07",
            status="confirmed",
            signal_type="trend_lane_pullback",
            signal_score=6.0,
            snap={"snap_support": 9.5},
        ),
        intraday,
        style="trend",
        daily_history=_make_daily_history_with_prev_close(10.0),
    )

    assert candidate.features.get("limit_up_touched", False) is False
    assert candidate.features.get("limit_up_closed", False) is False


def test_evaluate_rule_decision_skips_limit_up_check_without_daily_history():
    intraday = _make_intraday_df(start=10.9, end=11.0)
    candidate = evaluate_rule_decision(
        TailBuyCandidate(
            code="603928",
            name="兴业股份",
            signal_date="2026-07-07",
            status="confirmed",
            signal_type="spring",
            signal_score=6.0,
            snap={"snap_support": 10.5},
        ),
        intraday,
        style="trend",
    )

    assert "limit_up_touched" not in candidate.features


def test_llm_prompt_surfaces_daily_trap_pressure_gate():
    candidate = evaluate_rule_decision(
        TailBuyCandidate(
            code="002217",
            name="合力泰",
            signal_date="2026-04-20",
            status="confirmed",
            signal_type="sos",
            signal_score=6.0,
            snap={"snap_support": 9.8},
        ),
        _make_intraday_df(start=10.3, end=11.2, tail_boost=0.8, tail_volume_mult=1.8),
        style="trend",
        config=TailBuyStrategyConfig(
            naked_support_extension_pct=30.0,
            chase_day_ret_pct=20.0,
            chase_high_ret_pct=25.0,
        ),
        daily_history=_make_daily_trap_df(),
    )

    system_prompt, user_prompt = build_llm_prompt(candidate, style="trend")

    assert "daily_trap_pressure=true" in system_prompt
    assert "不能选择 BUY" in system_prompt
    assert "daily_trap_pressure=True" in user_prompt
    assert "daily_close_vs_ma20_pct" in user_prompt
    assert "日线" in user_prompt


def test_llm_prompt_surfaces_mainline_semantics_without_overriding_gates():
    candidate = TailBuyCandidate(
        code="300308",
        name="中际旭创",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="mainline",
        signal_score=86.0,
        candidate_lane="mainline",
        candidate_theme="光模块",
        candidate_phase="分歧机会",
        candidate_role="主线核心",
        mainline_score=0.86,
        theme_score=0.8,
        stock_role_score=0.82,
    )

    system_prompt, user_prompt = build_llm_prompt(candidate, style="trend")

    assert "只能原样使用" in system_prompt
    assert "不能覆盖规则硬闸" in system_prompt
    assert "theme=光模块, phase=分歧机会, role=主线核心" in user_prompt
    assert "mainline=0.86, theme=0.8, role=0.82" in user_prompt


def test_rule_scan_batch_applies_daily_trap_pressure_gate():
    class FakeTickFlow:
        def get_intraday_batch(self, symbols, *, period, count):
            assert period == "1m"
            assert count == 5000
            df = _make_intraday_df(start=10.3, end=11.2, tail_boost=0.8, tail_volume_mult=1.8)
            return {symbol: df for symbol in symbols}

        def get_klines_batch(self, symbols, *, period, count, adjust):
            assert period == "1d"
            assert count == 40
            assert adjust == "forward"
            return {symbol: _make_daily_trap_df() for symbol in symbols}

    candidate = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="sos",
        signal_score=6.0,
        snap={"snap_support": 9.8},
    )

    scanned = run_rule_scan_batch(
        [candidate],
        tickflow_client=FakeTickFlow(),
        style="trend",
        strategy_config=TailBuyStrategyConfig(
            naked_support_extension_pct=30.0,
            chase_day_ret_pct=20.0,
            chase_high_ret_pct=25.0,
        ),
        batch_size=20,
        deadline_at=datetime.now(TZ) + timedelta(seconds=60),
    )

    assert scanned[0].rule_decision == DECISION_WATCH
    assert scanned[0].features["daily_trap_pressure"] is True
    assert "日线" in "；".join(scanned[0].rule_reasons)


def test_rule_scan_batch_skips_daily_fetch_when_trap_gate_disabled():
    class FakeTickFlow:
        def get_intraday_batch(self, symbols, *, period, count):
            df = _make_intraday_df(start=10.3, end=11.2, tail_boost=0.8, tail_volume_mult=1.8)
            return {symbol: df for symbol in symbols}

        def get_klines_batch(self, *_args, **_kwargs):
            raise AssertionError("daily history should not be fetched when trap gate is disabled")

    candidate = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="sos",
        signal_score=6.0,
        snap={"snap_support": 9.8},
    )

    scanned = run_rule_scan_batch(
        [candidate],
        tickflow_client=FakeTickFlow(),
        style="trend",
        strategy_config=TailBuyStrategyConfig(
            daily_trap_gate_enabled=False,
            naked_support_extension_pct=30.0,
            chase_day_ret_pct=20.0,
            chase_high_ret_pct=25.0,
        ),
        batch_size=20,
        deadline_at=datetime.now(TZ) + timedelta(seconds=60),
    )

    assert scanned[0].fetch_error == ""
    assert "daily_trap_pressure" not in scanned[0].features


def test_merge_rule_and_llm_keeps_pending_buy_as_watch():
    c1 = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=5.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
    )
    c2 = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="pending",
        signal_type="sos",
        signal_score=4.0,
        rule_score=60.0,
        rule_decision=DECISION_WATCH,
        final_decision=DECISION_WATCH,
    )
    c3 = TailBuyCandidate(
        code="600000",
        name="浦发银行",
        signal_date="2026-04-20",
        status="pending",
        signal_type="sos",
        signal_score=2.0,
        rule_score=35.0,
        rule_decision=DECISION_SKIP,
        final_decision=DECISION_SKIP,
    )

    llm_map = {
        "002217": {
            "decision": DECISION_BUY,
            "reason": "尾盘再加速",
            "confidence": 0.76,
            "model_used": "nvidia-kimi:moonshot",
        },
        "301090": {
            "decision": DECISION_WATCH,
            "reason": "高位波动扩大",
            "confidence": 0.64,
            "model_used": "gemini:flash",
        },
    }
    merged = merge_rule_and_llm([c1, c2, c3], llm_map)
    by_code = {x.code: x for x in merged}

    assert by_code["002217"].llm_decision == DECISION_BUY
    assert by_code["002217"].final_decision == DECISION_WATCH
    assert "未二次确认" in by_code["002217"].llm_reason
    assert by_code["301090"].final_decision == DECISION_WATCH
    assert by_code["600000"].final_decision == DECISION_SKIP
    assert by_code["600000"].llm_decision is None
    assert by_code["002217"].llm_model_used.startswith("nvidia-kimi")


def test_tail_buy_strategy_config_from_env_can_disable_confirmation_gate(monkeypatch):
    monkeypatch.setenv("TAIL_BUY_CONFIRMED_ONLY_BUY", "0")
    config = tail_buy_strategy_config_from_env()

    item = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="pending",
        signal_type="sos",
        signal_score=4.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
    )
    merged = merge_rule_and_llm([item], {}, config=config)

    assert config.confirmed_only_buy is False
    assert merged[0].final_decision == DECISION_BUY


def test_merge_rule_and_llm_cannot_override_tail_hard_veto():
    item = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        rule_score=20.0,
        rule_decision=DECISION_SKIP,
        final_decision=DECISION_SKIP,
        features={"tail_blowoff_reversal": True},
    )

    merged = merge_rule_and_llm(
        [item],
        {
            "301090": {
                "decision": DECISION_BUY,
                "reason": "模型误判",
                "confidence": 0.9,
            }
        },
    )

    assert merged[0].final_decision == DECISION_SKIP
    assert merged[0].llm_decision is None
    assert "冲高回落" in "；".join(merged[0].rule_reasons)


def test_merge_rule_and_llm_cannot_override_soft_buy_gate():
    item = TailBuyCandidate(
        code="688620",
        name="安凯微",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="evr",
        signal_score=3.5,
        rule_score=68.0,
        rule_decision=DECISION_WATCH,
        final_decision=DECISION_WATCH,
        features={
            "bars": 180,
            "support_level": 13.9,
            "day_ret_pct": 14.0,
            "intraday_high_ret_pct": 15.0,
        },
    )

    merged = merge_rule_and_llm(
        [item],
        {
            "688620": {
                "decision": DECISION_BUY,
                "reason": "模型认为尾盘强",
                "confidence": 0.8,
            }
        },
    )

    assert merged[0].llm_decision == DECISION_BUY
    assert merged[0].final_decision == DECISION_WATCH
    assert merged[0].priority_score <= 100.0
    assert "日内涨幅过大" in merged[0].llm_reason


def test_merge_rule_and_llm_cannot_override_daily_trap_gate():
    item = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="sos",
        signal_score=6.0,
        rule_score=68.0,
        rule_decision=DECISION_WATCH,
        final_decision=DECISION_WATCH,
        features={
            "bars": 180,
            "support_level": 9.8,
            "day_low_breached_support": False,
            "close_below_support": False,
            "daily_trap_pressure": True,
            "daily_trap_reason": "日线放量上影(2.6x)",
        },
    )

    merged = merge_rule_and_llm(
        [item],
        {
            "002217": {
                "decision": DECISION_BUY,
                "reason": "尾盘再加速",
                "confidence": 0.8,
                "model_used": "test-route",
            }
        },
    )

    assert merged[0].llm_decision == DECISION_BUY
    assert merged[0].final_decision == DECISION_WATCH
    assert merged[0].priority_score == 71.0
    assert "日线放量上影" in merged[0].llm_reason


def test_compute_tail_features_handles_volume_lot_unit_for_vwap():
    df = _make_intraday_df(start=10.0, end=10.6, tail_boost=0.3, tail_volume_mult=1.3)
    # 模拟 TickFlow: volume 为"手"，amount 为"元"（需 volume*100 才接近真实股数换算）
    df["amount"] = df["close"] * df["volume"] * 100.0
    feats = compute_tail_features(df)
    assert feats["bars"] >= 60
    # 手→股换算发生在数据清洗层（ensure_intraday_df），VWAP 不再需要单独猜测缩放。
    assert feats["vwap_volume_scale"] == 1.0
    assert 9.5 < feats["vwap"] < 11.0
    assert feats["dist_vwap_pct"] > -10.0


def test_select_llm_overlay_candidates_prefilters_skip_and_low_score():
    items = [
        TailBuyCandidate(
            code="301090",
            name="华润材料",
            signal_date="2026-04-20",
            status="confirmed",
            signal_type="spring",
            signal_score=6.0,
            rule_score=82.0,
            rule_decision=DECISION_BUY,
        ),
        TailBuyCandidate(
            code="002217",
            name="合力泰",
            signal_date="2026-04-20",
            status="pending",
            signal_type="sos",
            signal_score=4.0,
            rule_score=61.0,
            rule_decision=DECISION_WATCH,
        ),
        TailBuyCandidate(
            code="600000",
            name="浦发银行",
            signal_date="2026-04-20",
            status="pending",
            signal_type="sos",
            signal_score=2.0,
            rule_score=58.0,
            rule_decision=DECISION_WATCH,
        ),
        TailBuyCandidate(
            code="000001",
            name="平安银行",
            signal_date="2026-04-20",
            status="pending",
            signal_type="sos",
            signal_score=1.0,
            rule_score=75.0,
            rule_decision=DECISION_SKIP,
        ),
    ]

    selected = select_llm_overlay_candidates(
        items,
        max_llm_symbols=10,
        min_rule_score=60.0,
        allowed_rule_decisions=(DECISION_BUY, DECISION_WATCH),
    )

    assert [x.code for x in selected] == ["301090", "002217"]


def test_build_tail_buy_markdown_can_append_extra_sections():
    c = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=90.0,
        rule_reasons=["尾盘走强"],
    )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[c],
        llm_total=1,
        llm_success=1,
        elapsed_seconds=10.0,
        extra_sections=["## 持仓动作建议（硬止损/结构减仓/洗盘观察）\n- 持仓数量: 1"],
    )
    assert "持仓动作建议（硬止损/结构减仓/洗盘观察）" in md
    assert "持仓数量: 1" in md


def test_build_tail_buy_markdown_surfaces_mainline_context():
    candidate = TailBuyCandidate(
        code="300308",
        name="中际旭创",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="mainline",
        signal_score=88.0,
        candidate_lane="mainline",
        candidate_status="强主线分歧",
        candidate_reasons={"theme": "光模块"},
        stock_role_score=0.82,
        rule_score=80.0,
        rule_decision=DECISION_WATCH,
        final_decision=DECISION_WATCH,
        priority_score=85.0,
    )

    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[candidate],
        llm_total=0,
        llm_success=0,
        elapsed_seconds=1.0,
        buy_only=False,
    )

    assert "光模块 / 分歧机会 / 主线核心" in md


def test_build_tail_buy_markdown_supports_custom_candidate_source():
    c = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=90.0,
        rule_reasons=["尾盘走强"],
    )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[c],
        llm_total=1,
        llm_success=1,
        elapsed_seconds=10.0,
        candidate_source="signal_pending + recommendation_tracking (2026-04-22)",
    )
    assert "signal_pending + recommendation_tracking (2026-04-22)" in md


def test_build_tail_buy_markdown_surfaces_policy_weights():
    c = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="lps",
        signal_score=6.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=90.0,
        rule_reasons=["尾盘走强"],
    )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[c],
        llm_total=1,
        llm_success=1,
        elapsed_seconds=10.0,
        policy_weights={"lps": 0.5, "launchpad": 1.2},
        policy_weight_meta={
            "source": "远端",
            "report_date": "2026-07-04",
            "horizon": "5",
            "age_days": 0,
            "execution_policy": "shadow",
            "execution_scope": "tail_buy_and_funnel_shadow",
            "next_action": "manual_review_dynamic_on",
            "formal_dynamic_allowed": True,
            "backtest_confirmation_text": "待复核(need backtest)",
            "promotion_checklist_summary": "样本=通过；回测=待复核",
            "tail_buy_weights_active": True,
            "funnel_shadow_weights_active": True,
            "funnel_formal_weights_active": False,
        },
    )

    assert (
        "- 归因调权: launchpad×1.20↑；lps×0.50↓"
        "（远端, 报告=2026-07-04, 周期=h5, 距今=0天, 策略=shadow 对照(shadow), 下一步=进入人工晋级评审（非正式生效）, 范围=尾盘+漏斗shadow, 回测=待复核(need backtest), 晋级=样本=通过；回测=待复核）"
    ) in md


def test_build_tail_buy_markdown_surfaces_selection_governance_without_weights():
    c = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="shadow_added",
        signal_score=6.0,
        rule_score=80.0,
        rule_decision=DECISION_WATCH,
        final_decision=DECISION_WATCH,
        priority_score=80.0,
        rule_reasons=["候选源观察"],
    )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[c],
        llm_total=0,
        llm_success=0,
        elapsed_seconds=10.0,
        policy_weight_meta={
            "selection_action_count": 1,
            "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
        },
        buy_only=False,
    )

    assert ("- 归因调权: 无；候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75") in md


def test_build_tail_buy_markdown_surfaces_item_policy_adjustment():
    c = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="lps",
        signal_score=6.0,
        rule_score=40.0,
        rule_decision=DECISION_WATCH,
        final_decision=DECISION_WATCH,
        priority_score=43.0,
        rule_reasons=["尾盘走强", "回踩不破", "第三条普通原因"],
        features={
            "policy_weight_signal": "lps",
            "policy_weight_multiplier": 0.5,
            "policy_weight_old_score": 80.0,
            "policy_weight_new_score": 40.0,
        },
    )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[c],
        llm_total=0,
        llm_success=0,
        elapsed_seconds=10.0,
        policy_weights={"lps": 0.5},
        buy_only=False,
    )

    assert "归因治理调权(lps) x0.50: 80.0->40.0" in md


def test_build_tail_buy_markdown_keeps_daily_trap_reason_visible():
    c = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="sos",
        signal_score=6.0,
        rule_score=68.0,
        rule_decision=DECISION_WATCH,
        final_decision=DECISION_WATCH,
        priority_score=71.0,
        rule_reasons=["尾盘站上VWAP", "尾段量能结构健康", "日线放量上影(2.6x)"],
        features={"daily_trap_reason": "日线放量上影(2.6x)"},
    )

    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[c],
        llm_total=1,
        llm_success=1,
        elapsed_seconds=10.0,
    )

    assert "日线放量上影(2.6x)" in md
    assert "尾盘站上VWAP；尾段量能结构健康；日线放量上影(2.6x)" in md


def test_build_tail_buy_markdown_splits_high_risk_momentum_buy():
    executable = TailBuyCandidate(
        code="603713",
        name="密尔克卫",
        signal_date="2026-07-01",
        status="confirmed",
        signal_type="sos",
        signal_score=6.0,
        rule_score=90.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=100.0,
        rule_reasons=["尾盘回踩再起"],
    )
    risky = TailBuyCandidate(
        code="600378",
        name="昊华科技",
        signal_date="2026-07-01",
        status="confirmed",
        signal_type="rec_momentum_continuation",
        signal_score=6.0,
        rule_score=90.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=100.0,
        rule_reasons=["尾盘封板"],
    )

    md = build_tail_buy_markdown(
        now_text="2026-07-01 14:57:00",
        target_signal_date="2026-07-01",
        market_reminder="NORMAL/NORMAL",
        candidates=[executable, risky],
        llm_total=2,
        llm_success=2,
        elapsed_seconds=10.0,
    )

    assert "BUY=2（可执行买入1 / 观察买入1）" in md
    assert md.find("603713 密尔克卫") < md.find("BUY（观察买入：高位动能默认不买）")
    assert "600378 昊华科技" in md
    assert "高位动能票，仅观察买入，默认不买" in md


def test_build_tail_buy_markdown_splits_limit_up_buy():
    """涨停股无法按现价挂单，即便信号类型正常也应降级到观察买入分区，而非可执行买入。"""
    executable = TailBuyCandidate(
        code="300649",
        name="杭州园林",
        signal_date="2026-07-07",
        status="confirmed",
        signal_type="trend_lane_pullback",
        signal_score=6.0,
        rule_score=97.8,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=100.0,
        rule_reasons=["回踩再企稳"],
    )
    limit_up = TailBuyCandidate(
        code="603928",
        name="兴业股份",
        signal_date="2026-07-07",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        rule_score=88.6,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=100.0,
        rule_reasons=["弹簧确认"],
        features={"limit_up_touched": True, "limit_up_closed": True},
    )

    md = build_tail_buy_markdown(
        now_text="2026-07-08 14:44:00",
        target_signal_date="2026-07-07",
        market_reminder="NORMAL/NORMAL",
        candidates=[executable, limit_up],
        llm_total=2,
        llm_success=2,
        elapsed_seconds=10.0,
    )

    assert "BUY=2（可执行买入1 / 观察买入1）" in md
    assert md.find("300649 杭州园林") < md.find("BUY（观察买入：高位动能默认不买）")
    assert "603928 兴业股份" in md
    assert "当日已涨停，无法按现价挂单，仅观察买入" in md


def test_build_tail_buy_markdown_post_close_review_labels_next_day_plan():
    c = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-06-29",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=90.0,
        rule_reasons=["尾盘走强"],
    )
    md = build_tail_buy_markdown(
        now_text="2026-06-29 17:30:00",
        target_signal_date="2026-06-29",
        market_reminder="NEUTRAL/NORMAL",
        candidates=[c],
        llm_total=1,
        llm_success=1,
        elapsed_seconds=10.0,
        report_mode="post_close_review",
    )

    assert "盘后尾盘复核" in md
    assert "明日观察买入" in md
    assert "BUY=明日观察买入" in md


def test_build_tail_buy_markdown_can_prepend_extra_sections():
    c = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=90.0,
        rule_reasons=["尾盘走强"],
    )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[c],
        llm_total=1,
        llm_success=1,
        elapsed_seconds=10.0,
        extra_sections=["## 持仓动作建议（硬止损/结构减仓/洗盘观察）\n- 持仓数量: 1"],
        extra_sections_first=True,
    )
    assert md.find("持仓动作建议（硬止损/结构减仓/洗盘观察）") < md.find("## BUY（可执行买入）")


def test_normalize_signal_score_lps_inverted():
    assert _normalize_signal_score(0.2, "lps") > 6.0
    assert _normalize_signal_score(0.5, "lps") > 2.0
    assert _normalize_signal_score(0.65, "lps") == 0.0


def test_normalize_signal_score_sos_scales():
    assert _normalize_signal_score(2.0, "sos") == 0.0
    assert 4.5 < _normalize_signal_score(4.0, "sos") < 5.5
    assert _normalize_signal_score(6.0, "sos") == 10.0


def test_auto_style_selects_by_signal_type():
    strong_df = _make_intraday_df(start=10.0, end=10.9, tail_boost=0.8, tail_volume_mult=2.0)
    spring_c = TailBuyCandidate(
        code="301090",
        name="T",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=5.0,
        snap={"snap_support": 9.8},
    )
    sos_c = TailBuyCandidate(
        code="002217",
        name="T",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="sos",
        signal_score=4.0,
        snap={"snap_support": 9.8},
    )
    spring_out = evaluate_rule_decision(spring_c, strong_df)
    sos_out = evaluate_rule_decision(sos_c, strong_df)
    assert spring_out.rule_score > 0
    assert sos_out.rule_score > 0


def test_holding_tail_action_adds_clear_signal_even_if_losing(monkeypatch):
    from workflows import tail_buy_holding_portfolio, tail_buy_utils
    from workflows import tail_buy_holdings as holdings_workflow

    class FakeTickFlow:
        def get_quotes(self, symbols):
            return {symbol: {"last_price": 9.5} for symbol in symbols}

        def get_intraday_batch(self, chunk, *, period, count):
            assert period == "1m"
            assert count == 5000
            df = _make_intraday_df(start=10.0, end=10.6, tail_boost=0.2, tail_volume_mult=2.0)
            return {symbol: df for symbol in chunk}

    monkeypatch.setattr(
        tail_buy_holding_portfolio,
        "load_portfolio_state",
        lambda _portfolio_id: {
            "state_signature": "sig",
            "positions": [{"code": "000001", "name": "平安银行", "shares": 1000, "cost": 10.0}],
        },
    )
    signal = TailBuyCandidate(
        code="000001",
        name="平安银行",
        signal_date="2026-05-20",
        status="confirmed",
        signal_type="sos",
        signal_score=6.0,
    )

    holdings, _limit_hit, _meta = holdings_workflow.analyze_holdings_actions(
        tickflow_client=FakeTickFlow(),
        portfolio_id="USER_LIVE:test",
        signal_map={"000001": signal},
        style="auto",
        intraday_batch_size=20,
        stop_config=holdings_workflow.HoldingStopConfig(hard_stop_pct=8.0),
        strategy_config=TailBuyStrategyConfig(),
        deadline_at=datetime.now(tail_buy_utils.TZ) + timedelta(seconds=60),
    )

    assert holdings[0].action == holdings_workflow.HOLDING_ACTION_ADD
    assert "尾盘结构延续走强" in "；".join(holdings[0].reasons)


def test_analyze_holdings_actions_skips_daily_history_fetch_when_atr_disabled(monkeypatch):
    from workflows import tail_buy_holding_portfolio
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingStopConfig

    class FakeTickFlow:
        def get_quotes(self, symbols):
            return {symbol: {"last_price": 9.0} for symbol in symbols}

        def get_intraday_batch(self, chunk, *, period, count):
            df = _make_intraday_df(start=9.5, end=9.0, tail_boost=-0.2, tail_volume_mult=1.5)
            return {symbol: df for symbol in chunk}

        def get_klines_batch(self, *_args, **_kwargs):
            raise AssertionError("daily history should not be fetched when ATR stop is disabled")

    monkeypatch.setattr(
        tail_buy_holding_portfolio,
        "load_portfolio_state",
        lambda _portfolio_id: {
            "state_signature": "sig",
            "positions": [{"code": "000001", "name": "平安银行", "shares": 1000, "cost": 10.0}],
        },
    )

    holdings, _limit_hit, _meta = holdings_workflow.analyze_holdings_actions(
        tickflow_client=FakeTickFlow(),
        portfolio_id="USER_LIVE:test",
        signal_map={},
        style="conservative",
        intraday_batch_size=20,
        stop_config=HoldingStopConfig(hard_stop_pct=8.0, atr_enabled=False),
        strategy_config=TailBuyStrategyConfig(),
        deadline_at=datetime.now(TZ) + timedelta(seconds=60),
    )

    assert holdings[0].code == "000001"


def test_analyze_holdings_actions_uses_atr_stop_to_avoid_hard_stop_trim(monkeypatch):
    from workflows import tail_buy_holding_portfolio
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingStopConfig

    # 现价9.0，固定止损=10*(1-8%)=9.2 会被触发（现价<止损线）；
    # 开启ATR后（ATR=1.0*2.0=2.0跌幅缓冲）应放宽止损线到7.5，现价9.0不再触发硬止损。
    class FakeTickFlow:
        def get_quotes(self, symbols):
            return {symbol: {"last_price": 9.0} for symbol in symbols}

        def get_intraday_batch(self, chunk, *, period, count):
            df = _make_intraday_df(start=9.0, end=9.0, bars=10, tail_boost=0.0, tail_volume_mult=1.0)
            return {symbol: df for symbol in chunk}

        def get_klines_batch(self, symbols, *, period, count, adjust):
            assert period == "1d"
            assert adjust == "forward"
            return {symbol: _make_atr_daily_history(atr_value=1.0) for symbol in symbols}

    monkeypatch.setattr(
        tail_buy_holding_portfolio,
        "load_portfolio_state",
        lambda _portfolio_id: {
            "state_signature": "sig",
            "positions": [{"code": "000001", "name": "平安银行", "shares": 1000, "cost": 10.0}],
        },
    )

    holdings, _limit_hit, _meta = holdings_workflow.analyze_holdings_actions(
        tickflow_client=FakeTickFlow(),
        portfolio_id="USER_LIVE:test",
        signal_map={},
        style="conservative",
        intraday_batch_size=20,
        stop_config=HoldingStopConfig(hard_stop_pct=8.0, atr_enabled=True, atr_multiplier=2.0, atr_max_relax_pct=15.0),
        strategy_config=TailBuyStrategyConfig(),
        deadline_at=datetime.now(TZ) + timedelta(seconds=60),
    )

    assert holdings[0].risk_tag != "hard_stop"


def test_holding_weak_tail_washout_stays_hold():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingAdvice

    advice = HoldingAdvice(code="000001", name="平安银行", cost=10.0, current_price=9.45, pnl_pct=-5.5)
    features = {
        "day_low": 9.1,
        "close_pos": 0.66,
        "dist_vwap_pct": -0.7,
        "last30_ret_pct": -1.0,
        "drop_from_high_pct": -1.9,
        "tail30_volume_share": 0.3,
        "tail_blowoff_reversal": False,
        "reclaim_vwap": False,
        "strong_hold_vwap": False,
    }

    holdings_workflow._resolve_scored_holding_action(
        advice,
        features,
        DECISION_SKIP,
        ["尾盘跌回VWAP下方", "尾盘30分钟转弱"],
        None,
        "",
        9.2,
    )

    assert advice.action == holdings_workflow.HOLDING_ACTION_HOLD
    assert advice.risk_tag == "washout"
    assert "疑似洗盘" in "；".join(advice.reasons)


def test_holding_confirmed_breakdown_trims():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingAdvice

    advice = HoldingAdvice(code="000001", name="平安银行", cost=10.0, current_price=9.35, pnl_pct=-6.5)
    features = {
        "day_low": 9.3,
        "close_pos": 0.22,
        "dist_vwap_pct": -1.4,
        "last30_ret_pct": -1.2,
        "drop_from_high_pct": -3.1,
        "tail30_volume_share": 0.34,
        "tail_blowoff_reversal": False,
        "close_below_support": False,
    }

    holdings_workflow._resolve_scored_holding_action(
        advice,
        features,
        DECISION_SKIP,
        ["尾盘跌回VWAP下方", "尾盘30分钟转弱"],
        None,
        "",
        9.2,
    )

    assert advice.action == holdings_workflow.HOLDING_ACTION_TRIM
    assert advice.risk_tag == "confirmed_breakdown"


def _make_frozen_board_df(price: float, bars: int = 180) -> pd.DataFrame:
    """构造一字涨跌停当天的分钟线：开=高=低=收，全天几乎无波动。"""
    idx = pd.date_range(start=datetime(2026, 4, 21, 9, 30), periods=bars, freq="1min", tz="Asia/Shanghai")
    flat = pd.Series([price] * bars)
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": flat.values,
            "high": flat.values,
            "low": flat.values,
            "close": flat.values,
            "volume": pd.Series([50.0] * bars).values,
            "amount": (flat * 50.0).values,
        }
    )


def test_holding_one_word_limit_down_short_circuits_to_washout():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingAdvice

    prev_close = 10.0
    limit_down_price = round(prev_close * 0.9, 2)
    advice = HoldingAdvice(code="000001", name="平安银行", cost=10.5)
    df_1m = _make_frozen_board_df(limit_down_price)

    holdings_workflow._apply_scored_holding_advice(
        advice=advice,
        df_1m=df_1m,
        quote={"prev_close": prev_close},
        signal_item=None,
        style="conservative",
        effective_stop=0.0,
        strategy_config=tail_buy_strategy_config_from_env(),
    )

    assert advice.limit_move_desc
    assert "一字跌停" in advice.limit_move_desc
    assert advice.action == holdings_workflow.HOLDING_ACTION_HOLD
    assert advice.risk_tag == "washout"
    assert advice.limit_move_desc in "；".join(advice.reasons)


def test_holding_hard_stop_takes_priority_over_limit_move():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingAdvice

    prev_close = 10.0
    limit_down_price = round(prev_close * 0.9, 2)
    advice = HoldingAdvice(code="000001", name="平安银行", cost=12.0)
    df_1m = _make_frozen_board_df(limit_down_price)

    holdings_workflow._apply_scored_holding_advice(
        advice=advice,
        df_1m=df_1m,
        quote={"prev_close": prev_close},
        signal_item=None,
        style="conservative",
        effective_stop=9.5,
        strategy_config=tail_buy_strategy_config_from_env(),
    )

    assert advice.action == holdings_workflow.HOLDING_ACTION_TRIM
    assert advice.risk_tag == "hard_stop"


def test_resolve_effective_stop_atr_disabled_uses_hard_pct_only():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingStopConfig

    stop = holdings_workflow._resolve_effective_stop(
        10.0,
        None,
        HoldingStopConfig(hard_stop_pct=8.0, atr_enabled=False),
        current_price=9.0,
        daily_history=_make_daily_trap_df(),
    )

    assert stop == pytest.approx(9.2)


def test_resolve_effective_stop_atr_relaxes_when_wider_than_hard_pct():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingStopConfig

    # 固定止损=10*(1-8%)=9.2；current_price=9.5, ATR=1.0, multiplier=2.0 => atr_stop=7.5，比固定线更宽松，
    # 且放宽上限设为30%（floor=7.0）足够容纳 atr_stop，最终采用 atr_stop=7.5。
    daily_history = _make_atr_daily_history(atr_value=1.0)
    stop = holdings_workflow._resolve_effective_stop(
        10.0,
        None,
        HoldingStopConfig(hard_stop_pct=8.0, atr_enabled=True, atr_multiplier=2.0, atr_max_relax_pct=30.0),
        current_price=9.5,
        daily_history=daily_history,
    )

    assert stop == pytest.approx(7.5)
    assert stop < 9.2


def test_resolve_effective_stop_atr_relax_capped_by_max_relax_pct():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingStopConfig

    # ATR 波动率极大 => atr_stop 远低于放宽上限；应被夹到 cost*(1-15%)=8.5，而不是无限放宽。
    daily_history = _make_atr_daily_history(atr_value=5.0)
    stop = holdings_workflow._resolve_effective_stop(
        10.0,
        None,
        HoldingStopConfig(hard_stop_pct=8.0, atr_enabled=True, atr_multiplier=2.0, atr_max_relax_pct=15.0),
        current_price=9.5,
        daily_history=daily_history,
    )

    assert stop == pytest.approx(8.5)


def test_resolve_effective_stop_atr_never_tightens_base_stop():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingStopConfig

    # ATR 很小 => atr_stop 比固定止损线更严格；此时应维持固定止损线（不收紧）。
    daily_history = _make_atr_daily_history(atr_value=0.05)
    stop = holdings_workflow._resolve_effective_stop(
        10.0,
        None,
        HoldingStopConfig(hard_stop_pct=8.0, atr_enabled=True, atr_multiplier=2.0, atr_max_relax_pct=15.0),
        current_price=9.5,
        daily_history=daily_history,
    )

    assert stop == pytest.approx(9.2)


def test_resolve_effective_stop_atr_falls_back_without_daily_history():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingStopConfig

    stop = holdings_workflow._resolve_effective_stop(
        10.0,
        None,
        HoldingStopConfig(hard_stop_pct=8.0, atr_enabled=True, atr_multiplier=2.0, atr_max_relax_pct=15.0),
        current_price=9.5,
        daily_history=None,
    )

    assert stop == pytest.approx(9.2)


def test_holding_normal_volatile_day_not_flagged_as_limit_move():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingAdvice

    advice = HoldingAdvice(code="000001", name="平安银行", cost=10.0)
    df_1m = _make_intraday_df(start=9.6, end=9.45)

    holdings_workflow._apply_scored_holding_advice(
        advice=advice,
        df_1m=df_1m,
        quote={"prev_close": 10.0},
        signal_item=None,
        style="conservative",
        effective_stop=0.0,
        strategy_config=tail_buy_strategy_config_from_env(),
    )

    assert advice.limit_move_desc == ""
    assert advice.risk_tag != "washout" or advice.limit_move_desc == ""


def test_holding_markdown_splits_washout_from_trim():
    from workflows import tail_buy_holdings as holdings_workflow
    from workflows.tail_buy_holding_models import HoldingAdvice

    wash = HoldingAdvice(code="000001", name="平安银行", action="HOLD", risk_tag="washout", reasons=["疑似洗盘"])
    trim = HoldingAdvice(code="000002", name="万科A", action="TRIM", risk_tag="hard_stop", reasons=["跌破风控位"])

    md = holdings_workflow.build_holdings_markdown(
        holdings=[wash, trim],
        portfolio_meta="portfolio=demo",
        tickflow_limit_hit=False,
    )

    assert "持仓动作建议（硬止损/结构减仓/洗盘观察）" in md
    assert "TRIM（硬止损/确认破位，优先处理）" in md
    assert "WASH（疑似洗盘/回踩测试，不直接卖）" in md
    assert "000001 平安银行" in md


def test_build_tail_buy_markdown_truncates_error_items_over_limit():
    items = []
    for i in range(7):
        items.append(
            TailBuyCandidate(
                code=f"60000{i}",
                name=f"样本{i}",
                signal_date="2026-04-20",
                status="pending",
                signal_type="sos",
                signal_score=1.0,
                rule_score=0.0,
                rule_decision=DECISION_SKIP,
                final_decision=DECISION_SKIP,
                priority_score=-20.0,
                fetch_error=f"ERR-{i}",
                rule_reasons=[f"ERR-{i}"],
            )
        )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=items,
        llm_total=0,
        llm_success=0,
        elapsed_seconds=10.0,
        max_error_items_per_block=5,
    )
    assert md.count("ERR-") == 5
    assert "其余 2 只报错标的已省略" in md


def test_tail_buy_history_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "test.db")
    from integrations import local_db

    local_db._conn = None  # reset singleton
    local_db.init_db()

    from integrations.local_db import load_tail_buy_history, save_tail_buy_results

    rows = [
        {
            "code": "301090",
            "name": "华润材料",
            "run_date": "2026-04-25",
            "signal_date": "2026-04-24",
            "signal_type": "spring",
            "status": "confirmed",
            "final_decision": "BUY",
            "rule_score": 78.5,
            "priority_score": 90.5,
            "rule_reasons": '["尾盘走强"]',
            "llm_decision": "BUY",
            "llm_reason": "强势回踩",
            "initial_price": 10.88,
            "current_price": 10.88,
            "change_pct": 0.0,
            "price_updated_at": "2026-04-25T14:55:00+08:00",
            "last_close": 10.88,
            "vwap": 10.41,
            "dist_vwap_pct": 4.5,
            "last30_ret_pct": 1.2,
            "features_json": '{"last_close":10.88}',
        },
        {
            "code": "002217",
            "name": "合力泰",
            "run_date": "2026-04-25",
            "signal_date": "2026-04-24",
            "signal_type": "sos",
            "status": "pending",
            "final_decision": "WATCH",
            "rule_score": 55.0,
            "priority_score": 58.0,
            "rule_reasons": '["量能一般"]',
            "llm_decision": "WATCH",
            "llm_reason": "",
        },
    ]
    saved = save_tail_buy_results(rows)
    assert saved == 2

    all_records = load_tail_buy_history()
    assert len(all_records) == 2

    buy_only = load_tail_buy_history(decision="BUY")
    assert len(buy_only) == 1
    assert buy_only[0]["code"] == "301090"
    assert buy_only[0]["initial_price"] == 10.88
    assert buy_only[0]["current_price"] == 10.88
    assert buy_only[0]["change_pct"] == 0.0
    assert buy_only[0]["last_close"] == 10.88
    assert buy_only[0]["dist_vwap_pct"] == 4.5

    by_date = load_tail_buy_history(run_date="2026-04-25")
    assert len(by_date) == 2

    empty = load_tail_buy_history(run_date="2020-01-01")
    assert len(empty) == 0

    local_db._conn = None  # cleanup
