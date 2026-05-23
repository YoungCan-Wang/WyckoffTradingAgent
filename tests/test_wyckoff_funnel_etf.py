from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

import scripts.wyckoff_funnel as funnel
from scripts.wyckoff_funnel import (
    _append_etf_section,
    _append_formal_l4_sections,
    _merge_trigger_maps,
    _promote_l2_bypass_for_ai,
    _rank_etf_candidates,
    _rank_l2_bypass_pool,
    _split_selected_tracks,
)


def _frame(step: float, last_volume: float) -> pd.DataFrame:
    dates = pd.date_range("2026-04-01", periods=30, freq="B")
    close = pd.Series([100.0 + i * step for i in range(30)])
    volume = pd.Series([100.0] * 29 + [last_volume])
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "close": close,
            "volume": volume,
        }
    )


def test_run_funnel_job_passes_l2_channel_map_to_l4(monkeypatch):
    channel_map = {"000001": "趋势延续", "000002": "加速突破"}
    df_map = {"000001": _frame(0.2, 100.0), "000002": _frame(0.1, 100.0)}
    calls: list[tuple[list[str], dict[str, str] | None]] = []
    _patch_funnel_job_inputs(monkeypatch, df_map)
    _patch_funnel_job_layers(monkeypatch, channel_map, calls)

    triggers, metrics = funnel.run_funnel_job()

    assert calls == [(["000001"], channel_map), (["000002"], channel_map)]
    assert triggers["trend_pullback"] == [("000001", 0.4)]
    assert metrics["l2_bypass_triggers"]["trend_pullback"] == [("000002", 0.4)]


def _patch_funnel_job_inputs(monkeypatch, df_map: dict[str, pd.DataFrame]) -> None:
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
    monkeypatch.setattr(funnel, "_resolve_funnel_end_calendar_day", lambda: date(2026, 5, 22))
    monkeypatch.setattr(
        funnel,
        "_resolve_trading_window",
        lambda **_kwargs: SimpleNamespace(start_trade_date=date(2026, 4, 1), end_trade_date=date(2026, 5, 22)),
    )
    monkeypatch.setattr(
        funnel,
        "_resolve_symbol_pool_from_env",
        lambda: (list(df_map), {"000001": "Alpha", "000002": "Beta"}, {"pool_main": 2}),
    )
    monkeypatch.setattr(funnel, "fetch_sector_map", lambda: {"000001": "科技", "000002": "科技"})
    monkeypatch.setattr(funnel, "fetch_concept_map", lambda: {})
    monkeypatch.setattr(funnel, "fetch_concept_heat", lambda: [])
    monkeypatch.setattr(funnel, "detect_theme_lines", lambda **_kwargs: [])
    monkeypatch.setattr(funnel, "fetch_market_cap_map", lambda: {})
    monkeypatch.setattr(funnel, "_stock_name_map", lambda: {"000001": "Alpha", "000002": "Beta"})
    monkeypatch.setattr(funnel, "_load_benchmark_indices", lambda *_args: (_frame(0.1, 100.0), _frame(0.1, 100.0)))
    monkeypatch.setattr(funnel, "fetch_all_ohlcv", lambda **_kwargs: (df_map, {"fetch_ok": 2}))
    monkeypatch.setattr(funnel, "_dump_full_fetch_snapshot", lambda **_kwargs: "")
    monkeypatch.setattr(funnel, "_run_etf_enhancement", lambda *_args, **_kwargs: ([], {}, {}, [], []))
    monkeypatch.setattr(funnel, "_calc_market_breadth", lambda *_args: {})
    monkeypatch.setattr(funnel, "_analyze_benchmark_and_tune_cfg", lambda *_args, **_kwargs: _benchmark_context())


def _patch_funnel_job_layers(monkeypatch, channel_map: dict[str, str], calls: list) -> None:
    def fake_layer4(symbols, _df_map, _cfg, *, channel_map=None, **_kwargs):
        calls.append((list(symbols), channel_map))
        return {"trend_pullback": [(symbols[0], 0.4)]} if symbols else {"trend_pullback": []}

    monkeypatch.setattr(funnel, "layer1_filter", lambda symbols, *_args, **_kwargs: symbols)
    monkeypatch.setattr(funnel, "layer2_strength_detailed", lambda *_args, **_kwargs: (["000001"], channel_map, []))
    monkeypatch.setattr(funnel, "layer3_sector_resonance", lambda symbols, *_args, **_kwargs: (symbols, ["科技"]))
    monkeypatch.setattr(funnel, "analyze_sector_rotation", lambda *_args, **_kwargs: {"headline": "", "state_map": {}})
    monkeypatch.setattr(funnel, "layer4_triggers", fake_layer4)
    monkeypatch.setattr(funnel, "detect_markup_stage", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(funnel, "detect_accum_stage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(funnel, "layer5_exit_signals", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(funnel, "_rank_l3_candidates", lambda **kwargs: (kwargs["l3_symbols"], {}))


def _benchmark_context() -> dict[str, object]:
    return {
        "regime": "NEUTRAL",
        "close": 100.0,
        "ma50": 99.0,
        "ma200": 95.0,
        "ma50_slope_5d": 0.1,
        "recent3_pct": 1.0,
        "recent3_cum_pct": 1.0,
        "tuned": False,
    }


def test_rank_etf_candidates_orders_by_strength():
    rows = _rank_etf_candidates(
        ["512880", "512480"],
        {
            "512880": _frame(0.1, 100.0),
            "512480": _frame(1.0, 280.0),
        },
        {"512880": "证券", "512480": "半导体"},
        {"512880": "吸筹通道", "512480": "主升通道+点火破局"},
    )

    assert [row["code"] for row in rows] == ["512480", "512880"]
    assert rows[0]["name"] == "半导体ETF"
    assert rows[0]["ret20"] > rows[1]["ret20"]


def test_append_etf_section_renders_compact_rows():
    rows = [
        {
            "code": "512480",
            "name": "半导体ETF",
            "score": 12.3,
            "ret3": 2.1,
            "ret20": 10.5,
            "vol_ratio": 1.8,
            "channel": "主升通道",
        }
    ]
    lines: list[str] = []

    _append_etf_section(lines, {"pool": 2, "fetched": 2, "l2_passed": 1}, rows)

    text = "\n".join(lines)
    assert "ETF强势池" in text
    assert "512480 半导体ETF" in text
    assert "3日+2.1%" in text


def test_append_formal_l4_sections_renders_all_hits_and_marks_ai():
    lines: list[str] = []
    scores = {"000001": 6.0, "000002": 3.0, "000003": 12.0}

    _append_formal_l4_sections(
        lines,
        ["000003", "000001", "000002"],
        ["000002"],
        {"000001": "平安银行", "000002": "万科A", "000003": "国农科技"},
        {"000001": ["sos"], "000002": ["lps"], "000003": ["sos", "evr"]},
        lambda code: scores[code],
    )

    text = "\n".join(lines)
    assert "【🔥 多信号共振】1 只" in text
    assert "【⚡ SOS 量价点火】1 只" in text
    assert "【🔄 LPS 缩量回踩】1 只" in text
    assert "000001 平安银行" in text
    assert "000002 万科A  3.00  →AI" in text


def test_split_selected_tracks_preserves_order_and_accum_only_hits():
    trend, accum = _split_selected_tracks(
        ["000001", "000002", "000003", "000004"],
        {
            "000001": ["sos"],
            "000002": ["lps"],
            "000003": ["spring", "evr"],
            "000004": ["compression"],
        },
    )

    assert trend == ["000001", "000003", "000004"]
    assert accum == ["000002"]


def test_merge_trigger_maps_keeps_bypass_l4_hits():
    merged = _merge_trigger_maps(
        {"lps": [("000001", 1.0)], "evr": [("000002", 2.0)]},
        {"lps": [("000001", 9.0), ("000003", 3.0)]},
    )

    assert merged["lps"] == [("000001", 1.0), ("000003", 3.0)]
    assert merged["evr"] == [("000002", 2.0)]


def test_promote_l2_bypass_for_ai_assigns_tracks_and_scores():
    selected = ["000001"]
    trend = ["000001"]
    accum: list[str] = []
    score_map: dict[str, float] = {}

    added = _promote_l2_bypass_for_ai(
        selected,
        trend,
        accum,
        ["000002", "000003"],
        {"000002": 4.0, "000003": 8.0},
        {"000002": ["lps"], "000003": ["evr"]},
        score_map,
    )

    assert added == 2
    assert selected == ["000001", "000003", "000002"]
    assert trend == ["000001", "000003"]
    assert accum == ["000002"]
    assert score_map["000002"] == 4.0


def test_rank_l2_bypass_pool_orders_by_score_then_code():
    ranked = _rank_l2_bypass_pool(
        ["000003", "000001", "000002", "000002"],
        {"000001": 5.0, "000002": 8.0, "000003": 8.0},
    )

    assert ranked == ["000002", "000003", "000001"]


def test_promote_l2_bypass_for_ai_respects_budget(monkeypatch):
    monkeypatch.setattr(funnel, "FUNNEL_L2_BYPASS_AI_CAP", 2)
    selected: list[str] = []
    trend: list[str] = []
    accum: list[str] = []
    score_map: dict[str, float] = {}

    added = funnel._promote_l2_bypass_for_ai(
        selected,
        trend,
        accum,
        ["000001", "000002", "000003"],
        {"000001": 1.0, "000002": 3.0, "000003": 2.0},
        {"000001": ["evr"], "000002": ["evr"], "000003": ["evr"]},
        score_map,
    )

    assert added == 2
    assert selected == ["000002", "000003"]
