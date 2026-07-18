from __future__ import annotations

from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from core import backtest_replay as replay_mod
from core.a_share_entry_research import AShareEntryResearchPolicy
from core.backtest_execution import ExitSimulationConfig
from core.backtest_replay import BacktestReplayConfig, build_signal_ledger, replay_backtest, replay_signal_ledger
from core.mainline_engine import MainlineEngineConfig
from core.wyckoff_engine import FunnelConfig, FunnelResult


def _hist() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [date(2026, 1, day) for day in range(1, 6)],
            "open": [10.0, 11.0, 12.0, 13.0, 14.0],
            "high": [10.5, 11.5, 12.5, 13.5, 14.5],
            "low": [9.5, 10.5, 11.5, 12.5, 13.5],
            "close": [10.2, 11.2, 12.2, 13.2, 14.2],
            "volume": [1000, 1100, 1200, 1300, 1400],
            "amount": [10_000, 11_000, 12_000, 13_000, 14_000],
            "pct_chg": [0, 1, 1, 1, 1],
        }
    )


def _result() -> FunnelResult:
    return FunnelResult(
        layer1_symbols=["000001"],
        layer2_symbols=["000001"],
        layer3_symbols=["000001"],
        top_sectors=[],
        triggers={"sos": [("000001", 2.0)]},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={"000001": "点火破局"},
        leader_radar_symbols=[],
        leader_radar_rows=[],
    )


def _config() -> BacktestReplayConfig:
    return BacktestReplayConfig(
        trading_days=3,
        hold_days=1,
        board="all",
        top_n=1,
        selection_mode="all_formal_l4",
        full_formal_l4_max=10,
        regime_filter=False,
        execution_regime_gate="off",
        pending_mode="off",
        pending_merge_order="funnel_first",
        abc_filter=False,
        entry_price_mode="open",
        entry_price_time="14:55",
        entry_price_fallback="close",
        buy_friction_pct=0.0,
        sell_friction_pct=0.0,
        max_atr_hold_days=120,
        exit=ExitSimulationConfig(
            exit_mode="close_only",
            stop_loss_pct=0.0,
            take_profit_pct=0.0,
            trailing_stop_pct=0.0,
            trailing_activate_pct=0.0,
            sltp_priority="stop_first",
            atr_period=14,
            atr_multiplier=2.0,
            atr_hard_stop_pct=-9.0,
        ),
    )


def test_replay_backtest_generates_t1_trades(monkeypatch) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr("core.backtest_replay.calc_market_breadth", lambda _df_map: {})
    monkeypatch.setattr(
        "core.backtest_replay.analyze_benchmark_and_tune_cfg", lambda *_args, **_kwargs: {"regime": "NEUTRAL"}
    )

    def fake_run_funnel(**kwargs):
        calls.update(kwargs)
        return _result()

    monkeypatch.setattr("core.backtest_replay.run_funnel", fake_run_funnel)
    cfg = FunnelConfig(trading_days=3)
    cfg.ma_long = 2
    replay_cfg = replace(
        _config(),
        concept_map={"000001": ["CPO"]},
        concept_heat=[{"name": "CPO", "pct": 3.2}],
        financial_map={"000001": {"roe": 12}},
        mainline_config=MainlineEngineConfig(max_ai_candidates=2),
    )

    replay = replay_backtest(
        all_df_map={"000001": _hist()},
        bench_df=_hist(),
        trade_dates=[date(2026, 1, day) for day in range(1, 6)],
        name_map={"000001": "平安银行"},
        market_cap_map={},
        sector_map={},
        base_cfg=cfg,
        config=replay_cfg,
    )

    assert replay.eval_days == 2
    assert replay.signal_days == 2
    assert replay.pending_confirmed_total == 0
    assert [record.trigger for record in replay.records] == ["sos", "sos"]
    assert replay.records[0].entry_date == date(2026, 1, 3)
    assert replay.records[0].exit_date == date(2026, 1, 4)
    assert replay.records[0].ret_pct == pytest.approx(10.0)
    assert replay.records[0].signal_confirmed is False
    assert calls["concept_map"] == {"000001": ["CPO"]}
    assert calls["concept_heat"] == [{"name": "CPO", "pct": 3.2}]
    assert calls["financial_map"] == {"000001": {"roe": 12}}
    assert calls["mainline_config"].max_ai_candidates == 2


def test_replay_progress_reports_elapsed_and_eta(monkeypatch, caplog) -> None:
    events: list[tuple[str, str, float]] = []
    monkeypatch.setattr(replay_mod, "monotonic", lambda: 1800.0)
    caplog.set_level("INFO", logger="core.backtest_replay")

    replay_mod._report_progress(19, 100, 7, lambda *args: events.append(args), started_at=0.0)

    assert events == [("回放交易", "20/100, signals=7, elapsed=30m, eta=2h00m", pytest.approx(0.52))]
    assert "elapsed=30m, eta=2h00m" in caplog.text


def test_history_end_positions_match_direct_slices() -> None:
    histories = {"000001": _hist(), "000002": _hist().copy()}
    trade_dates = [date(2026, 1, day) for day in range(1, 6)]
    positions = replay_mod._build_history_end_positions(histories, trade_dates)

    for idx, signal_date in enumerate(trade_dates):
        indexed = replay_mod._day_df_map(
            histories,
            signal_date,
            3,
            1,
            date_index=idx,
            history_end_positions=positions,
        )
        direct = replay_mod._day_df_map(histories, signal_date, 3, 1)
        for code in histories:
            pd.testing.assert_frame_equal(indexed[code], direct[code])
            assert indexed[code].attrs["_wyckoff_date_sorted"] is True


def test_shared_signal_ledger_matches_independent_replays(monkeypatch) -> None:
    monkeypatch.setattr("core.backtest_replay.calc_market_breadth", lambda _df_map: {})
    monkeypatch.setattr(
        "core.backtest_replay.analyze_benchmark_and_tune_cfg", lambda *_args, **_kwargs: {"regime": "NEUTRAL"}
    )
    monkeypatch.setattr("core.backtest_replay.run_funnel", lambda **_kwargs: _result())
    all_df_map = {"000001": _hist()}
    trade_dates = [date(2026, 1, day) for day in range(1, 6)]
    cfg = FunnelConfig(trading_days=3)
    cfg.ma_long = 2
    short_config = _config()
    long_config = replace(short_config, hold_days=2)
    ledger = build_signal_ledger(
        all_df_map=all_df_map,
        bench_df=_hist(),
        trade_dates=trade_dates,
        name_map={"000001": "平安银行"},
        market_cap_map={},
        sector_map={},
        base_cfg=cfg,
        config=short_config,
        max_idx=len(trade_dates) - short_config.hold_days - 1,
    )

    for config in (short_config, long_config):
        shared = replay_signal_ledger(
            ledger=ledger,
            all_df_map=all_df_map,
            trade_dates=trade_dates,
            name_map={"000001": "平安银行"},
            config=config,
        )
        independent = replay_backtest(
            all_df_map=all_df_map,
            bench_df=_hist(),
            trade_dates=trade_dates,
            name_map={"000001": "平安银行"},
            market_cap_map={},
            sector_map={},
            base_cfg=cfg,
            config=config,
        )
        assert shared == independent


def test_replay_backtest_ignores_deprecated_regime_filter(monkeypatch) -> None:
    result = FunnelResult(
        layer1_symbols=["000001", "000002"],
        layer2_symbols=["000001", "000002"],
        layer3_symbols=["000001", "000002"],
        top_sectors=[],
        triggers={"sos": [("000001", 8.0), ("000002", 7.0)]},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={"000001": "点火破局", "000002": "点火破局"},
        leader_radar_symbols=[],
        leader_radar_rows=[],
    )
    monkeypatch.setattr("core.backtest_replay.calc_market_breadth", lambda _df_map: {})
    monkeypatch.setattr(
        "core.backtest_replay.analyze_benchmark_and_tune_cfg", lambda *_args, **_kwargs: {"regime": "RISK_ON"}
    )
    monkeypatch.setattr("core.backtest_replay.run_funnel", lambda **_kwargs: result)
    replay_cfg = replace(_config(), top_n=0, regime_filter=True)
    hist = _hist()
    cfg = FunnelConfig(trading_days=3)
    cfg.ma_long = 2

    replay = replay_backtest(
        all_df_map={"000001": hist, "000002": hist},
        bench_df=hist,
        trade_dates=[date(2026, 1, day) for day in range(1, 6)],
        name_map={"000001": "平安银行", "000002": "万科A"},
        market_cap_map={},
        sector_map={},
        base_cfg=cfg,
        config=replay_cfg,
    )

    assert {record.code for record in replay.records} == {"000001", "000002"}


def test_replay_backtest_live_execution_gate_blocks_risk_on(monkeypatch) -> None:
    monkeypatch.setattr("core.backtest_replay.calc_market_breadth", lambda _df_map: {})
    monkeypatch.setattr(
        "core.backtest_replay.analyze_benchmark_and_tune_cfg", lambda *_args, **_kwargs: {"regime": "RISK_ON"}
    )
    monkeypatch.setattr("core.backtest_replay.run_funnel", lambda **_kwargs: _result())
    cfg = FunnelConfig(trading_days=3)
    cfg.ma_long = 2

    replay = replay_backtest(
        all_df_map={"000001": _hist()},
        bench_df=_hist(),
        trade_dates=[date(2026, 1, day) for day in range(1, 6)],
        name_map={"000001": "平安银行"},
        market_cap_map={},
        sector_map={},
        base_cfg=cfg,
        config=replace(_config(), execution_regime_gate="live"),
    )

    assert replay.records == []
    assert replay.regime_day_counts == {"RISK_ON": 2}
    assert replay.regime_blocked_signal_days == 2
    assert replay.regime_blocked_candidates == 2


def test_replay_backtest_neutral_only_gate_blocks_caution(monkeypatch) -> None:
    assert replay_mod._execution_regime_allows("NEUTRAL", "neutral_only") is True
    assert replay_mod._execution_regime_allows("CAUTION", "neutral_only") is False
    assert replay_mod._execution_regime_allows("RISK_ON", "off") is True


@pytest.mark.parametrize("regime", ["CAUTION", "PANIC_REPAIR_CONFIRMED", "CRASH_LEFT_PROBE"])
def test_live_gate_limits_probe_only_regime_to_one_candidate(regime: str) -> None:
    selected = replay_mod._RankedSelection(
        ["000001", "000002"],
        {"000001": 90.0, "000002": 80.0},
        {"000001": "Trend", "000002": "Accum"},
        {},
        frozenset({"000001", "000002"}),
    )

    limited, blocked = replay_mod._limit_probe_only_selection(selected, regime, "live")

    assert limited.codes == ["000001"]
    assert limited.confirmed_codes == frozenset({"000001"})
    assert blocked == 1


def test_confirmed_signals_dedupes_code_and_keeps_best_score() -> None:
    class Pending:
        def write(self, *_args, **_kwargs):
            return None

        def tick(self, *_args, **_kwargs):
            return [
                {"code": "000001", "score": 30.0, "track": "Trend", "signal_type": "sos"},
                {"code": "000001", "score": 90.0, "track": "Accum", "signal_type": "spring"},
                {"code": "000001", "score": 20.0, "track": "Trend", "signal_type": "evr"},
            ]

    ctx = replay_mod._DayContext(
        idx=0,
        signal_date=date(2026, 1, 1),
        entry_target_date=date(2026, 1, 2),
        day_df_map={"000001": _hist()},
        name_map={"000001": "平安银行"},
        day_cfg=FunnelConfig(trading_days=3),
        result=_result(),
        regime="NEUTRAL",
    )

    confirmed = replay_mod._confirmed_signals(ctx, Pending(), {})

    assert confirmed.codes == ["000001"]
    assert confirmed.score_map == {"000001": 90.0}
    assert confirmed.track_map == {"000001": "Accum"}
    assert confirmed.trigger_map == {"000001": "spring"}


def test_confirmed_signals_rank_codes_by_best_score() -> None:
    class Pending:
        def write(self, *_args, **_kwargs):
            return None

        def tick(self, *_args, **_kwargs):
            return [
                {"code": "000002", "score": 20.0, "signal_type": "sos"},
                {"code": "000001", "score": 90.0, "signal_type": "spring"},
                {"code": "000003", "score": 90.0, "signal_type": "lps"},
            ]

    ctx = replay_mod._DayContext(
        idx=0,
        signal_date=date(2026, 1, 1),
        entry_target_date=date(2026, 1, 2),
        day_df_map={"000001": _hist()},
        name_map={"000001": "平安银行"},
        day_cfg=FunnelConfig(trading_days=3),
        result=_result(),
        regime="NEUTRAL",
    )

    confirmed = replay_mod._confirmed_signals(ctx, Pending(), {})

    assert confirmed.codes == ["000001", "000003", "000002"]


def test_confirmed_signals_apply_a_share_research_filter_and_score() -> None:
    class Pending:
        def write(self, *_args, **_kwargs):
            return None

        def tick(self, *_args, **_kwargs):
            return [
                {"code": "EVR", "score": 100.0, "signal_type": "evr"},
                {"code": "SOS", "score": 90.0, "signal_type": "sos"},
                {"code": "TREND", "score": 5.0, "signal_type": "trend_pullback"},
            ]

    ctx = replay_mod._DayContext(
        idx=0,
        signal_date=date(2026, 1, 1),
        entry_target_date=date(2026, 1, 2),
        day_df_map={"000001": _hist()},
        name_map={},
        day_cfg=FunnelConfig(trading_days=3),
        result=_result(),
        regime="CAUTION",
    )
    policy = AShareEntryResearchPolicy(blocked_confirmed_signals=("evr",), calibrate_confirmed_score=True)

    confirmed = replay_mod._confirmed_signals(ctx, Pending(), {}, policy)

    assert confirmed.codes == ["TREND", "SOS"]
    assert "EVR" not in confirmed.score_map


def test_confirmed_signals_require_breadth_for_neutral_research_variant() -> None:
    class Pending:
        def __init__(self):
            self.written = False
            self.ticked = False

        def write(self, *_args, **_kwargs):
            self.written = True

        def tick(self, *_args, **_kwargs):
            self.ticked = True
            return [{"code": "000001", "score": 10.0, "signal_type": "spring"}]

    ctx = replay_mod._DayContext(
        idx=0,
        signal_date=date(2026, 1, 1),
        entry_target_date=date(2026, 1, 2),
        day_df_map={"000001": _hist()},
        name_map={},
        day_cfg=FunnelConfig(trading_days=3),
        result=_result(),
        regime="NEUTRAL",
    )

    pending = Pending()
    confirmed = replay_mod._confirmed_signals(
        ctx,
        pending,
        {},
        AShareEntryResearchPolicy(require_neutral_breadth_confirmation=True),
    )

    assert confirmed.codes == []
    assert pending.written is True
    assert pending.ticked is True


def test_confirmed_signals_treats_invalid_scores_as_zero() -> None:
    class Pending:
        def write(self, *_args, **_kwargs):
            return None

        def tick(self, *_args, **_kwargs):
            return [
                {"code": "BAD", "score": "bad", "track": "Trend", "signal_type": "sos"},
                {"code": "INF", "score": float("inf"), "track": "Trend", "signal_type": "sos"},
                {"code": "NAN", "score": float("nan"), "track": "Trend", "signal_type": "sos"},
                {"code": "GOOD", "score": float("nan"), "track": "Trend", "signal_type": "sos"},
                {"code": "GOOD", "score": 90.0, "track": "Accum", "signal_type": "spring"},
            ]

    ctx = replay_mod._DayContext(
        idx=0,
        signal_date=date(2026, 1, 1),
        entry_target_date=date(2026, 1, 2),
        day_df_map={"000001": _hist()},
        name_map={"000001": "平安银行"},
        day_cfg=FunnelConfig(trading_days=3),
        result=_result(),
        regime="NEUTRAL",
    )

    confirmed = replay_mod._confirmed_signals(ctx, Pending(), {})

    assert confirmed.codes == ["GOOD", "BAD", "INF", "NAN"]
    assert confirmed.score_map == {"BAD": 0.0, "INF": 0.0, "NAN": 0.0, "GOOD": 90.0}
    assert confirmed.track_map["GOOD"] == "Accum"
    assert confirmed.trigger_map["GOOD"] == "spring"


def test_confirmed_signals_infer_track_from_signal_type_when_track_missing() -> None:
    class Pending:
        def write(self, *_args, **_kwargs):
            return None

        def tick(self, *_args, **_kwargs):
            return [{"code": "000001", "score": 90.0, "signal_type": "spring"}]

    ctx = replay_mod._DayContext(
        idx=0,
        signal_date=date(2026, 1, 1),
        entry_target_date=date(2026, 1, 2),
        day_df_map={"000001": _hist()},
        name_map={"000001": "平安银行"},
        day_cfg=FunnelConfig(trading_days=3),
        result=_result(),
        regime="NEUTRAL",
    )

    confirmed = replay_mod._confirmed_signals(ctx, Pending(), {})

    assert confirmed.score_map == {"000001": 90.0}
    assert confirmed.track_map == {"000001": "Accum"}
    assert confirmed.trigger_map == {"000001": "spring"}


def test_name_score_map_prefers_higher_confirmed_source_name() -> None:
    result = _result()._replace(
        candidate_entries=[
            {"code": "000001", "entry_type": "launchpad", "score": 80.0},
            {"code": "000002", "entry_type": "tight_base", "score": 70.0},
        ]
    )
    confirmed = replay_mod._ConfirmedSignals(
        codes=["000001"],
        score_map={"000001": 90.0},
        track_map={"000001": "Accum"},
        trigger_map={"000001": "spring"},
    )

    got = replay_mod._name_score_map(result, confirmed)

    assert got["000001"] == (90.0, "spring(确认)")
    assert got["000002"] == (70.0, "tight_base")


def test_name_score_map_keeps_higher_score_but_uses_confirmed_name() -> None:
    result = _result()._replace(candidate_entries=[{"code": "000001", "entry_type": "launchpad", "score": 100.0}])
    confirmed = replay_mod._ConfirmedSignals(
        codes=["000001"],
        score_map={"000001": 10.0},
        track_map={"000001": "Accum"},
        trigger_map={"000001": "spring"},
    )

    assert replay_mod._name_score_map(result, confirmed, prefer_confirmed=True)["000001"] == (
        100.0,
        "spring(确认)",
    )


def test_name_score_map_treats_invalid_candidate_scores_as_zero() -> None:
    result = _result()._replace(
        candidate_entries=[
            {"code": "000001", "entry_type": "launchpad", "score": float("inf")},
            {"code": "000002", "entry_type": "tight_base", "score": float("nan")},
        ]
    )
    confirmed = replay_mod._ConfirmedSignals(codes=[], score_map={}, track_map={}, trigger_map={})

    got = replay_mod._name_score_map(result, confirmed)

    assert got["000001"] == (2.0, "sos")
    assert got["000002"] == (0.0, "tight_base")


def test_candidate_entry_duplicate_metadata_stays_consistent_in_replay(monkeypatch) -> None:
    monkeypatch.setattr("core.backtest_replay.calc_market_breadth", lambda _df_map: {})
    monkeypatch.setattr(
        "core.backtest_replay.analyze_benchmark_and_tune_cfg", lambda *_args, **_kwargs: {"regime": "NEUTRAL"}
    )

    def fake_run_funnel(**_kwargs):
        return _result()._replace(
            triggers={},
            candidate_entries=[
                {"code": "000001", "track": "future_leader", "entry_type": "launchpad", "score": 80.0},
                {"code": "000001", "track": "accumulation", "entry_type": "spring", "score": 100.0},
            ],
        )

    monkeypatch.setattr("core.backtest_replay.run_funnel", fake_run_funnel)
    replay_cfg = replace(_config(), selection_mode="tradeable_l4")
    cfg = FunnelConfig(trading_days=3)
    cfg.ma_long = 2

    replay = replay_backtest(
        all_df_map={"000001": _hist()},
        bench_df=_hist(),
        trade_dates=[date(2026, 1, day) for day in range(1, 6)],
        name_map={"000001": "平安银行"},
        market_cap_map={},
        sector_map={},
        base_cfg=cfg,
        config=replay_cfg,
    )

    assert replay.records[0].score == 100.0
    assert replay.records[0].track == "Accum"
    assert replay.records[0].trigger == "spring"


def test_low_score_confirmed_signal_does_not_downgrade_funnel_candidate(monkeypatch) -> None:
    class Pending:
        def __init__(self):
            self.written = False

        def write(self, *_args, **_kwargs):
            self.written = True

        def tick(self, *_args, **_kwargs):
            return [{"code": "000001", "score": 20.0, "track": "Trend", "signal_type": "evr"}]

    monkeypatch.setattr("core.backtest_replay.calc_market_breadth", lambda _df_map: {})
    monkeypatch.setattr(
        "core.backtest_replay.analyze_benchmark_and_tune_cfg", lambda *_args, **_kwargs: {"regime": "NEUTRAL"}
    )
    monkeypatch.setattr(
        "core.backtest_replay.run_funnel",
        lambda **_kwargs: _result()._replace(
            triggers={},
            candidate_entries=[
                {"code": "000001", "track": "accumulation", "entry_type": "spring", "score": 100.0},
            ],
        ),
    )
    monkeypatch.setattr("core.backtest_replay.PendingPool", Pending)
    replay_cfg = replace(_config(), selection_mode="tradeable_l4", pending_mode="both")
    cfg = FunnelConfig(trading_days=3)
    cfg.ma_long = 2

    replay = replay_backtest(
        all_df_map={"000001": _hist()},
        bench_df=_hist(),
        trade_dates=[date(2026, 1, day) for day in range(1, 6)],
        name_map={"000001": "平安银行"},
        market_cap_map={},
        sector_map={},
        base_cfg=cfg,
        config=replay_cfg,
    )

    assert replay.records[0].score == 100.0
    assert replay.records[0].track == "Accum"
    assert replay.records[0].trigger == "spring"
    assert replay.records[0].signal_confirmed is True
