from __future__ import annotations

from datetime import date

import pandas as pd

import workflows.single_symbol_diagnosis as diag


def _daily_frame(rows: int = 230) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=rows, freq="B")
    close = pd.Series(range(rows), dtype="float64") * 0.1 + 20.0
    volume = pd.Series([1_000_000 + index * 1000 for index in range(rows)], dtype="float64")
    df = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "date_obj": [item.date() for item in dates],
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": volume,
            "amount": close * volume,
            "pct_chg": close.pct_change().fillna(0.0) * 100.0,
        }
    )
    return df


def test_detect_symbol_market_normalizes_cn_hk_us():
    assert diag.detect_symbol_market("603390") == diag.SymbolSpec("cn", "603390", "A股")
    assert diag.detect_symbol_market("700") == diag.SymbolSpec("hk", "00700.HK", "港股")
    assert diag.detect_symbol_market("AAPL") == diag.SymbolSpec("us", "AAPL.US", "美股")
    assert diag.detect_symbol_market("msft.us") == diag.SymbolSpec("us", "MSFT.US", "美股")


def test_evaluate_day_reports_selected_trigger(monkeypatch):
    symbol = diag.SymbolSpec("us", "AAPL.US", "美股")
    cfg = diag.config_for_symbol(symbol, 220)
    ctx = diag.ReplayContext({"AAPL.US": "Apple"}, {}, {}, None)

    monkeypatch.setattr(diag, "layer1_filter", lambda symbols, *_args, **_kwargs: symbols)
    monkeypatch.setattr(
        diag, "layer2_strength_detailed", lambda symbols, *_args, **_kwargs: (symbols, {"AAPL.US": "main"}, [])
    )
    monkeypatch.setattr(diag, "layer3_sector_resonance", lambda symbols, *_args, **_kwargs: (symbols, []))
    monkeypatch.setattr(diag, "layer4_triggers", lambda *_args, **_kwargs: {"sos": [("AAPL.US", 12.5)]})
    monkeypatch.setattr(diag, "candidate_lane_scores", lambda *_args, **_kwargs: {})

    row = diag.evaluate_day(symbol, _daily_frame(), ctx, cfg, date(2025, 11, 18))

    assert row.status == "SELECTED"
    assert row.failed_layer == "-"
    assert row.triggers == "SOS"
    assert "触发" in row.reason


def test_trigger_scores_treats_invalid_scores_as_zero() -> None:
    scores = diag.trigger_scores(
        {
            "sos": [
                ("AAPL.US", float("inf")),
                ("AAPL.US", float("nan")),
                ("AAPL.US", "bad"),
            ],
            "spring": [("AAPL.US", 12.5)],
            "lps": [("MSFT.US", 99.0)],
        },
        "AAPL.US",
    )

    assert scores == {"sos": 0.0, "spring": 12.5}


def test_candidate_lane_scores_treats_invalid_scores_as_zero(monkeypatch) -> None:
    symbol = diag.SymbolSpec("us", "AAPL.US", "美股")
    ctx = diag.ReplayContext({"AAPL.US": "Apple"}, {}, {}, None)
    monkeypatch.setattr(
        diag,
        "build_l1_candidate_lane_entries",
        lambda **_kwargs: [
            {"code": "AAPL.US", "signal_key": "launchpad", "score": float("inf")},
            {"code": "AAPL.US", "entry_type": "tight_base", "score": "bad"},
            {"code": "MSFT.US", "entry_type": "other", "score": 99.0},
        ],
    )

    scores = diag.candidate_lane_scores(symbol, _daily_frame(), ctx, ["AAPL.US"], [], {})

    assert scores == {"launchpad": 0.0, "tight_base": 0.0}


def test_evaluate_day_reports_l4_miss(monkeypatch):
    symbol = diag.SymbolSpec("us", "AAPL.US", "美股")
    cfg = diag.config_for_symbol(symbol, 220)
    ctx = diag.ReplayContext({"AAPL.US": "Apple"}, {}, {}, None)

    monkeypatch.setattr(diag, "layer1_filter", lambda symbols, *_args, **_kwargs: symbols)
    monkeypatch.setattr(diag, "layer2_strength_detailed", lambda symbols, *_args, **_kwargs: (symbols, {}, []))
    monkeypatch.setattr(diag, "layer3_sector_resonance", lambda symbols, *_args, **_kwargs: (symbols, []))
    monkeypatch.setattr(diag, "layer4_triggers", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(diag, "candidate_lane_scores", lambda *_args, **_kwargs: {})

    row = diag.evaluate_day(symbol, _daily_frame(), ctx, cfg, date(2025, 11, 18))

    assert row.status == "MISS"
    assert row.failed_layer == "L4"
    assert "未触发正式" in row.reason


def test_evaluate_day_slices_benchmark_to_replay_day(monkeypatch):
    symbol = diag.SymbolSpec("cn", "603390", "A股")
    cfg = diag.config_for_symbol(symbol, 220)
    bench_df = pd.DataFrame(
        {
            "date": ["2025-11-17", "2025-11-18", "2025-11-19"],
            "close": [3000.0, 3010.0, 2990.0],
        }
    )
    ctx = diag.ReplayContext({"603390": "603390"}, {}, {}, bench_df)
    seen: dict[str, pd.DataFrame] = {}

    monkeypatch.setattr(diag, "layer1_filter", lambda symbols, *_args, **_kwargs: symbols)

    def fake_layer2(symbols, _df_map, bench_arg, *_args, **_kwargs):
        seen["bench"] = bench_arg
        return symbols, {"603390": "main"}, []

    monkeypatch.setattr(diag, "layer2_strength_detailed", fake_layer2)
    monkeypatch.setattr(diag, "layer3_sector_resonance", lambda symbols, *_args, **_kwargs: (symbols, []))
    monkeypatch.setattr(diag, "layer4_triggers", lambda *_args, **_kwargs: {"sos": [("603390", 10.0)]})
    monkeypatch.setattr(diag, "diagnostic_loss_guard_reason", lambda *_args, **_kwargs: "")

    row = diag.evaluate_day(symbol, _daily_frame(), ctx, cfg, date(2025, 11, 18))

    assert row.status == "SELECTED"
    assert seen["bench"]["date"].tolist() == ["2025-11-17", "2025-11-18"]


def test_evaluate_day_reports_loss_guard_miss(monkeypatch):
    symbol = diag.SymbolSpec("cn", "603390", "A股")
    cfg = diag.config_for_symbol(symbol, 220)
    ctx = diag.ReplayContext({"603390": "603390"}, {}, {}, None)

    monkeypatch.setattr(diag, "layer1_filter", lambda symbols, *_args, **_kwargs: symbols)
    monkeypatch.setattr(
        diag, "layer2_strength_detailed", lambda symbols, *_args, **_kwargs: (symbols, {"603390": "main"}, [])
    )
    monkeypatch.setattr(diag, "layer3_sector_resonance", lambda symbols, *_args, **_kwargs: (symbols, []))
    monkeypatch.setattr(diag, "layer4_triggers", lambda *_args, **_kwargs: {"sos": [("603390", 10.0)]})
    monkeypatch.setattr(
        diag,
        "diagnostic_loss_guard_reason",
        lambda *_args, **_kwargs: "正式候选风控拦截: 右侧信号ABC不足",
    )

    row = diag.evaluate_day(symbol, _daily_frame(), ctx, cfg, date(2025, 11, 18))

    assert row.status == "MISS"
    assert row.failed_layer == "LOSS_GUARD"
    assert row.reason == "正式候选风控拦截: 右侧信号ABC不足"


def test_load_rps_histories_rejects_empty_cn_universe(monkeypatch):
    symbol = diag.SymbolSpec("cn", "603390", "A股")
    cfg = diag.config_for_symbol(symbol, 220)
    monkeypatch.setattr(diag, "load_rps_universe_histories", lambda *_args, **_kwargs: {})

    try:
        diag.load_required_rps_histories(symbol, date(2025, 1, 1), date(2025, 2, 1), cfg, False)
    except RuntimeError as exc:
        assert "RPS 全市场历史不足" in str(exc)
    else:
        raise AssertionError("empty RPS universe should fail before self-ranking")
