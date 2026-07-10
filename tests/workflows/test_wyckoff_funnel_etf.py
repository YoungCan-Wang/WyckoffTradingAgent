from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

import integrations.funnel_etf_data as etf_data
import workflows.funnel_ai_selection as funnel_ai_selection
import workflows.funnel_candidates as funnel_candidates
import workflows.funnel_data as funnel_data
import workflows.funnel_etf as etf_workflow
import workflows.funnel_layers as funnel_layers
import workflows.wyckoff_funnel as funnel
from core.candidate_policy import apply_loss_guard
from core.funnel_etf import append_etf_section, rank_etf_candidates
from core.funnel_report import signal_report_fields
from core.funnel_sections import append_formal_l4_sections
from core.funnel_selection import (
    merge_trigger_maps,
    promote_l2_bypass_for_ai,
    rank_l2_bypass_pool,
    should_force_quota_selection,
    split_selected_tracks,
)
from integrations.funnel_etf_data import load_etf_universe
from workflows.funnel_etf import fetch_etf_ohlcv, run_etf_enhancement


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
    monkeypatch.setattr(funnel_data, "_resolve_funnel_end_calendar_day", lambda: date(2026, 5, 22))
    monkeypatch.setattr(
        funnel_data,
        "resolve_trading_window",
        lambda **_kwargs: SimpleNamespace(start_trade_date=date(2026, 4, 1), end_trade_date=date(2026, 5, 22)),
    )
    monkeypatch.setattr(
        funnel_data,
        "resolve_symbol_pool_from_env",
        lambda: (list(df_map), {"000001": "Alpha", "000002": "Beta"}, {"pool_main": 2}),
    )
    monkeypatch.setattr(funnel_data, "fetch_sector_map", lambda: {"000001": "科技", "000002": "科技"})
    monkeypatch.setattr(funnel_data, "fetch_concept_map", lambda: {})
    monkeypatch.setattr(funnel_data, "fetch_concept_heat", lambda: [])
    monkeypatch.setattr(funnel_data, "detect_theme_lines", lambda **_kwargs: [])
    monkeypatch.setattr(funnel_data, "fetch_market_cap_map", lambda: {})
    monkeypatch.setattr(funnel_data, "load_stock_name_map", lambda: {"000001": "Alpha", "000002": "Beta"})
    monkeypatch.setattr(funnel_data, "_load_benchmark_indices", lambda *_args: (_frame(0.1, 100.0), _frame(0.1, 100.0)))
    monkeypatch.setattr(funnel_data, "fetch_all_ohlcv", lambda **_kwargs: (df_map, {"fetch_ok": 2}))
    monkeypatch.setattr(funnel_data, "dump_full_fetch_snapshot", lambda **_kwargs: "")
    monkeypatch.setattr(funnel_data, "run_etf_enhancement", lambda *_args, **_kwargs: ([], {}, {}, [], []))
    monkeypatch.setattr(funnel_data, "calc_market_breadth", lambda *_args: {})
    monkeypatch.setattr(funnel_data, "calc_market_money_flow", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(funnel_data, "calc_amount_distribution_health", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(funnel_data, "analyze_benchmark_and_tune_cfg", lambda *_args, **_kwargs: _benchmark_context())


def _patch_funnel_job_layers(monkeypatch, channel_map: dict[str, str], calls: list) -> None:
    def fake_layer4(symbols, _df_map, _cfg, *, channel_map=None, **_kwargs):
        calls.append((list(symbols), channel_map))
        return {"trend_pullback": [(symbols[0], 0.4)]} if symbols else {"trend_pullback": []}

    monkeypatch.setattr(funnel_layers, "layer1_filter", lambda symbols, *_args, **_kwargs: symbols)
    monkeypatch.setattr(
        funnel_layers, "layer2_strength_detailed", lambda *_args, **_kwargs: (["000001"], channel_map, [])
    )
    monkeypatch.setattr(
        funnel_layers, "layer3_sector_resonance", lambda symbols, *_args, **_kwargs: (symbols, ["科技"])
    )
    monkeypatch.setattr(
        funnel_layers, "analyze_sector_rotation", lambda *_args, **_kwargs: {"headline": "", "state_map": {}}
    )
    monkeypatch.setattr(funnel_layers, "layer4_triggers", fake_layer4)
    monkeypatch.setattr(funnel_layers, "detect_leader_radar", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(funnel_layers, "_safe_build_theme_radar", lambda **kwargs: {"trade_date": kwargs["trade_date"]})
    monkeypatch.setattr(funnel_layers, "_resolve_linked_theme_radar", lambda current, _trade_date: (current, "current"))
    monkeypatch.setattr(funnel, "layer4_triggers", fake_layer4)
    monkeypatch.setattr(funnel_candidates, "layer4_triggers", fake_layer4)
    monkeypatch.setattr(funnel_candidates, "detect_markup_stage", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(funnel_candidates, "detect_accum_stage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(funnel_candidates, "layer5_exit_signals", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(funnel_candidates, "rank_l3_candidates", lambda **kwargs: (kwargs["l3_symbols"], {}))


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


def test_load_financial_metrics_can_be_disabled_for_backfill(monkeypatch):
    monkeypatch.setenv("FUNNEL_SKIP_FINANCIAL_METRICS", "1")
    monkeypatch.setenv("TICKFLOW_API_KEY", "should-not-be-used")

    assert funnel_data._load_financial_metrics(["000001"]) == {}


def test_load_financial_metrics_reports_missing_tickflow_key(monkeypatch):
    from utils.progress import set_reporter

    events: list[tuple[str, str, float]] = []
    monkeypatch.delenv("FUNNEL_SKIP_FINANCIAL_METRICS", raising=False)
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
    set_reporter(lambda stage, detail, progress: events.append((stage, detail, progress)))
    try:
        assert funnel_data._load_financial_metrics(["000001"]) == {}
    finally:
        set_reporter(None)

    assert ("财务指标", "未配置 TickFlow，跳过", 0.20) in events


def test_load_reference_data_can_skip_financial_metrics_for_quick_scan(monkeypatch):
    from utils.progress import set_reporter

    events: list[tuple[str, str, float]] = []
    monkeypatch.setattr(funnel_data, "_load_market_metadata", lambda *_args: ({}, {}, [], {}, [], {}, [], {}))
    monkeypatch.setattr(
        funnel_data,
        "_load_financial_metrics",
        lambda _symbols: (_ for _ in ()).throw(AssertionError("financial metrics should be skipped")),
    )
    monkeypatch.setattr(funnel_data, "_load_stock_names", lambda: {"000001": "Alpha"})
    set_reporter(lambda stage, detail, progress: events.append((stage, detail, progress)))
    try:
        ref_data = funnel_data._load_reference_data(
            ["000001"],
            SimpleNamespace(end_trade_date=date(2026, 7, 2)),
            SimpleNamespace(),
            include_financial_metrics=False,
        )
    finally:
        set_reporter(None)

    assert ref_data.financial_map == {}
    assert ref_data.name_map == {"000001": "Alpha"}
    assert ("财务指标", "聊天快扫已跳过", 0.20) in events


def test_fetch_funnel_ohlcv_reports_overall_progress(monkeypatch):
    from utils.progress import set_reporter

    events: list[tuple[str, str, float]] = []
    df_map = {"000001": _frame(0.1, 100.0)}
    captured: dict[str, object] = {}
    pool = funnel_data.FunnelSymbolPool(
        symbols=["000001"],
        pool_name_map={"000001": "Alpha"},
        stats={},
        external_seed_cfg=SimpleNamespace(enabled=False, source="", symbols=[]),
        external_added_to_pool=0,
        main_count=1,
        chinext_count=0,
        star_count=0,
        bse_count=0,
        merged_count=1,
        st_excluded_count=0,
        total_batches=1,
    )

    def fake_fetch_all_ohlcv(**kwargs):
        captured.update(kwargs)
        return df_map, {"fetch_ok": 1, "fetch_fail": 0}

    monkeypatch.setattr(funnel_data, "fetch_all_ohlcv", fake_fetch_all_ohlcv)
    set_reporter(lambda stage, detail, progress: events.append((stage, detail, progress)))
    try:
        out, stats = funnel_data._fetch_funnel_ohlcv(
            pool,
            SimpleNamespace(start_trade_date=date(2026, 4, 1), end_trade_date=date(2026, 5, 22)),
            enforce_target_trade_date=True,
            direct_source=True,
            executor_mode="thread",
        )
    finally:
        set_reporter(None)

    assert out == df_map
    assert stats == {"fetch_ok": 1, "fetch_fail": 0}
    assert captured["symbols"] == ["000001"]
    assert captured["enforce_target_trade_date"] is True
    assert captured["direct_source"] is True
    assert captured["executor_mode"] == "thread"
    assert events == [
        ("日线拉取", "共1只/1批", 0.40),
        ("日线拉取", "成功=1，失败=0", 0.75),
    ]


def test_load_stock_names_reports_progress(monkeypatch):
    from utils.progress import set_reporter

    events: list[tuple[str, str, float]] = []
    monkeypatch.setattr(funnel_data, "load_stock_name_map", lambda: {"000001": "Alpha"})
    set_reporter(lambda stage, detail, progress: events.append((stage, detail, progress)))
    try:
        assert funnel_data._load_stock_names() == {"000001": "Alpha"}
    finally:
        set_reporter(None)

    assert ("股票名称", "加载代码名称映射", 0.25) in events
    assert ("股票名称", "加载1条", 0.28) in events


def test_rank_etf_candidates_orders_by_strength():
    rows = rank_etf_candidates(
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

    append_etf_section(lines, {"pool": 2, "fetched": 2, "l2_passed": 1}, rows)

    text = "\n".join(lines)
    assert "ETF强势池" in text
    assert "512480 半导体ETF" in text
    assert "3日+2.1%" in text


def test_load_etf_universe_parses_comments_and_tags(tmp_path):
    path = tmp_path / "etf_cn.txt"
    path.write_text("512480 半导体 # comment\nbad row\n510300 沪深300ETF\n", encoding="utf-8")

    codes, sectors = load_etf_universe(path)

    assert codes == ["512480", "510300"]
    assert sectors == {"512480": "半导体", "510300": "沪深300ETF"}


def test_fetch_etf_ohlcv_skips_without_data_source(monkeypatch):
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setattr(etf_workflow, "fetch_all_ohlcv", lambda **_kwargs: (_ for _ in ()).throw(AssertionError))

    assert fetch_etf_ohlcv(["512480"], SimpleNamespace()) == {}


def test_run_etf_enhancement_updates_maps(monkeypatch):
    df_map = {"512480": _frame(1.0, 280.0)}
    sector_map: dict[str, str] = {}
    all_df_map: dict[str, pd.DataFrame] = {}
    monkeypatch.setattr(etf_data, "load_etf_universe", lambda: (["512480"], {"512480": "半导体"}))
    monkeypatch.setattr(etf_workflow, "fetch_etf_ohlcv", lambda *_args, **_kwargs: df_map)
    monkeypatch.setattr(etf_workflow, "layer1_filter", lambda symbols, *_args, **_kwargs: symbols)
    monkeypatch.setattr(
        etf_workflow, "layer2_strength_detailed", lambda *_args, **_kwargs: (["512480"], {"512480": "主升通道"}, [])
    )

    syms, sectors, fetched, l2_passed, candidates = run_etf_enhancement(
        funnel.FunnelConfig(), SimpleNamespace(), None, sector_map, all_df_map
    )

    assert syms == ["512480"]
    assert sectors == {"512480": "半导体"}
    assert fetched == df_map
    assert l2_passed == ["512480"]
    assert candidates[0]["code"] == "512480"
    assert sector_map == {"512480": "半导体"}
    assert all_df_map == df_map


def test_append_formal_l4_sections_renders_all_hits_and_marks_ai():
    lines: list[str] = []
    scores = {"000001": 6.0, "000002": 3.0, "000003": 12.0}

    append_formal_l4_sections(
        lines,
        ["000003", "000001", "000002"],
        ["000002"],
        {"000001": "平安银行", "000002": "万科A", "000003": "国农科技"},
        {"000001": ["sos"], "000002": ["lps"], "000003": ["sos", "evr"]},
        lambda code: scores[code],
        confirmation_label=lambda code: "二次确认:A+C(2/3)" if code == "000002" else "",
    )

    text = "\n".join(lines)
    assert "【🔥 多信号共振】1 只" in text
    assert "【⚡ SOS 量价点火】1 只" in text
    assert "【🔄 LPS 缩量回踩】1 只" in text
    assert "000001 平安银行" in text
    assert "000002 万科A  3.00  →AI  二次确认:A+C(2/3)" in text


def test_split_selected_tracks_preserves_order_and_accum_only_hits():
    trend, accum = split_selected_tracks(
        ["000001", "000002", "000003", "000004"],
        {
            "000001": ["sos"],
            "000002": ["lps"],
            "000003": ["spring", "evr"],
            "000004": ["compression"],
        },
    )

    assert trend == ["000001", "000003"]
    assert accum == ["000002", "000004"]


def test_full_formal_ai_selection_respects_hard_cap(monkeypatch):
    monkeypatch.setattr(funnel_ai_selection, "FUNNEL_FULL_FORMAL_L4_MAX", 2)

    selected, trend, accum, score_map, policy = funnel_ai_selection.full_formal_ai_selection(
        ["000001", "000002", "000003"],
        {"000001": 3.0, "000002": 2.0, "000003": 1.0},
        {"000001": ["sos"], "000002": ["lps"], "000003": ["spring"]},
    )

    assert selected == ["000001", "000002"]
    assert trend == ["000001"]
    assert accum == ["000002"]
    assert score_map == {"000001": 3.0, "000002": 2.0}
    assert policy["total_cap"] == 2
    assert policy["formal_l4_total"] == 3
    assert policy["formal_l4_cap"] == 2


def test_full_formal_ai_selection_sanitizes_score_map(monkeypatch):
    monkeypatch.setattr(funnel_ai_selection, "FUNNEL_FULL_FORMAL_L4_MAX", 0)

    selected, _trend, _accum, score_map, _policy = funnel_ai_selection.full_formal_ai_selection(
        ["GOOD", "BAD", "INF", "NAN"],
        {"GOOD": 4.25, "BAD": "bad", "INF": float("inf"), "NAN": float("nan")},
        {"GOOD": ["sos"], "BAD": ["lps"], "INF": ["spring"], "NAN": ["compression"]},
    )

    assert selected == ["GOOD", "BAD", "INF", "NAN"]
    assert score_map == {"GOOD": 4.25, "BAD": 0.0, "INF": 0.0, "NAN": 0.0}


def test_policy_shadow_meta_sanitizes_scores():
    meta = funnel_ai_selection._policy_shadow_meta(
        True,
        ["000001", "000002", "000003"],
        ["000001"],
        ["000002"],
        {"000001": 3.5, "000002": "bad", "000003": float("nan")},
    )

    assert meta["shadow_score_map"] == {"000001": 3.5, "000002": 0.0, "000003": 0.0}


def test_merge_trigger_maps_keeps_bypass_l4_hits():
    merged = merge_trigger_maps(
        {"lps": [("000001", 1.0)], "evr": [("000002", 2.0)]},
        {"lps": [("000001", 9.0), ("000003", 3.0)]},
    )

    assert merged["lps"] == [("000001", 1.0), ("000003", 3.0)]
    assert merged["evr"] == [("000002", 2.0)]


def test_merge_trigger_maps_treats_invalid_scores_as_zero():
    merged = merge_trigger_maps(
        {"lps": [("000001", "bad"), ("000002", float("inf")), ("000003", float("nan"))]},
    )

    assert merged["lps"] == [("000001", 0.0), ("000002", 0.0), ("000003", 0.0)]


def test_promote_l2_bypass_for_ai_assigns_tracks_and_scores():
    selected = ["000001"]
    trend = ["000001"]
    accum: list[str] = []
    score_map: dict[str, float] = {}

    added = promote_l2_bypass_for_ai(
        selected,
        trend,
        accum,
        ["000002", "000003"],
        {"000002": 4.0, "000003": 8.0},
        {"000002": ["lps"], "000003": ["evr"]},
        score_map,
        enabled=True,
        cap=0,
    )

    assert added == 2
    assert selected == ["000001", "000003", "000002"]
    assert trend == ["000001", "000003"]
    assert accum == ["000002"]
    assert score_map["000002"] == 4.0


def test_rank_l2_bypass_pool_orders_by_score_then_code():
    ranked = rank_l2_bypass_pool(
        ["000003", "000001", "000002", "000002"],
        {"000001": 5.0, "000002": 8.0, "000003": 8.0},
    )

    assert ranked == ["000002", "000003", "000001"]


def test_rank_l2_bypass_pool_treats_invalid_scores_as_zero():
    ranked = rank_l2_bypass_pool(
        ["GOOD", "BAD", "INF", "NAN"],
        {"GOOD": 2.0, "BAD": "bad", "INF": float("inf"), "NAN": float("nan")},
    )

    assert ranked == ["GOOD", "BAD", "INF", "NAN"]


def test_promote_l2_bypass_for_ai_sanitizes_score_map():
    selected: list[str] = []
    trend: list[str] = []
    accum: list[str] = []
    score_map: dict[str, float] = {}

    added = promote_l2_bypass_for_ai(
        selected,
        trend,
        accum,
        ["000001"],
        {"000001": float("nan")},
        {"000001": ["evr"]},
        score_map,
        enabled=True,
        cap=1,
    )

    assert added == 1
    assert score_map == {"000001": 0.0}


def test_promote_l2_bypass_for_ai_respects_budget():
    selected: list[str] = []
    trend: list[str] = []
    accum: list[str] = []
    score_map: dict[str, float] = {}

    added = promote_l2_bypass_for_ai(
        selected,
        trend,
        accum,
        ["000001", "000002", "000003"],
        {"000001": 1.0, "000002": 3.0, "000003": 2.0},
        {"000001": ["evr"], "000002": ["evr"], "000003": ["evr"]},
        score_map,
        enabled=True,
        cap=2,
    )

    assert added == 2
    assert selected == ["000002", "000003"]


def test_promote_l2_bypass_for_ai_respects_total_cap():
    selected = ["000001"]
    trend = ["000001"]
    accum: list[str] = []
    score_map: dict[str, float] = {}

    added = promote_l2_bypass_for_ai(
        selected,
        trend,
        accum,
        ["000002", "000003"],
        {"000002": 3.0, "000003": 2.0},
        {"000002": ["evr"], "000003": ["evr"]},
        score_map,
        enabled=True,
        cap=0,
        total_cap=2,
    )

    assert added == 1
    assert selected == ["000001", "000002"]


def test_promote_l2_bypass_for_ai_blocks_defensive_regime():
    selected: list[str] = []
    trend: list[str] = []
    accum: list[str] = []
    score_map: dict[str, float] = {}

    added = promote_l2_bypass_for_ai(
        selected,
        trend,
        accum,
        ["000001", "000002"],
        {"000001": 9.0, "000002": 8.0},
        {"000001": ["evr"], "000002": ["lps"]},
        score_map,
        enabled=True,
        cap=2,
        regime="RISK_OFF",
    )

    assert added == 0
    assert selected == []
    assert trend == []
    assert accum == []


def test_defensive_regime_forces_quota_selection():
    assert should_force_quota_selection("CRASH", True, defensive_force_quota=True) is True
    assert should_force_quota_selection("BEAR_REBOUND", True, defensive_force_quota=True) is True
    assert should_force_quota_selection("PANIC_REPAIR", True, defensive_force_quota=True) is True
    assert should_force_quota_selection("RISK_ON", True, defensive_force_quota=True) is False


def test_loss_guard_keeps_risk_on_pure_momentum_after_confirmation_gate():
    selected = ["000001", "000002", "000003"]
    trend = ["000002", "000003"]
    accum = ["000001"]

    kept, trend_kept, accum_kept, dropped = apply_loss_guard(
        selected,
        trend,
        accum,
        regime="RISK_ON",
        code_to_trigger_keys={"000001": ["lps"], "000002": ["sos"], "000003": ["sos"]},
        code_to_total_score={"000001": 0.4, "000002": 6.0, "000003": 6.0},
        channel_map={"000002": "主升通道", "000003": "点火破局"},
        df_map={},
    )

    assert kept == ["000002", "000003"]
    assert trend_kept == ["000002", "000003"]
    assert accum_kept == []
    assert dropped == {"单LPS仅观察": 1}


def test_loss_guard_still_blocks_bear_rebound_pure_momentum():
    kept, trend_kept, accum_kept, dropped = apply_loss_guard(
        ["000001"],
        ["000001"],
        [],
        regime="BEAR_REBOUND",
        code_to_trigger_keys={"000001": ["sos"]},
        code_to_total_score={"000001": 8.0},
        channel_map={"000001": "主升通道"},
        df_map={},
    )

    assert kept == []
    assert trend_kept == []
    assert accum_kept == []
    assert dropped == {"BEAR_REBOUND纯趋势追涨": 1}


def test_loss_guard_keeps_neutral_point_ignition():
    kept, trend_kept, _accum_kept, dropped = apply_loss_guard(
        ["000001"],
        ["000001"],
        [],
        regime="NEUTRAL",
        code_to_trigger_keys={"000001": ["sos"]},
        code_to_total_score={"000001": 6.0},
        channel_map={"000001": "加速突破+点火破局"},
        df_map={},
    )

    assert kept == ["000001"]
    assert trend_kept == ["000001"]
    assert dropped == {}


def test_loss_guard_bear_rebound_bans_pure_lps_even_with_score():
    kept, trend_kept, accum_kept, dropped = apply_loss_guard(
        ["000001"],
        [],
        ["000001"],
        regime="BEAR_REBOUND",
        code_to_trigger_keys={"000001": ["lps"]},
        code_to_total_score={"000001": 8.0},
        channel_map={},
        df_map={},
    )

    assert kept == []
    assert trend_kept == []
    assert accum_kept == []
    assert dropped == {"单LPS仅观察": 1}


def test_select_base_ai_candidates_repair_mode_uses_empty_base_quota():
    selected, trend, accum, score_map, ai_policy, use_full = funnel_ai_selection.select_base_ai_candidates(
        metrics={},
        triggers={"sos": [("000001", 3.0)]},
        l3_ranked_symbols=["000001"],
        regime="BEAR_REBOUND",
        sector_map={},
        benchmark_context={},
        formal_sorted_codes=["000001"],
        code_to_best_score={"000001": 3.0},
        code_to_trigger_keys={"000001": ["sos"]},
        full_mode_enabled=True,
    )

    assert selected == []
    assert trend == []
    assert accum == []
    assert score_map == {"000001": 25.0}
    assert ai_policy["total_cap"] == 8
    assert ai_policy["trend_quota"] == 0
    assert ai_policy["accum_quota"] == 0
    assert use_full is False


def test_promote_review_candidates_caps_repair_mainline_review(monkeypatch):
    monkeypatch.setattr(funnel_ai_selection, "FUNNEL_MAINLINE_MAX_AI_CANDIDATES", 3)
    selected: list[str] = []
    trend: list[str] = []
    accum: list[str] = []

    bypass_added, strategic_added, theme_added, mainline_added = funnel_ai_selection.promote_review_candidates(
        selected,
        trend,
        accum,
        {
            "l2_bypass": [],
            "strategic_l2_bypass": [],
            "strategic_accum": set(),
            "formal_hit": set(),
            "mainline": ["000001", "000002", "000003"],
        },
        code_to_total_score={"000001": 5.0, "000002": 4.0, "000003": 3.0},
        code_to_trigger_keys={"000001": ["mainline"], "000002": ["mainline"], "000003": ["mainline"]},
        score_map={},
        ai_policy={"total_cap": 8},
        use_full_ai_selection=False,
        theme_bonus_map={},
        regime="PANIC_REPAIR",
    )

    assert (bypass_added, strategic_added, theme_added, mainline_added) == (0, 0, 0, 2)
    assert selected == ["000001", "000002"]


def test_promote_review_candidates_blocks_neutral_bypass_but_allows_theme(monkeypatch):
    monkeypatch.setattr(funnel_ai_selection, "FUNNEL_L2_BYPASS_AI_ENABLED", True)
    monkeypatch.setattr(funnel_ai_selection, "FUNNEL_STRATEGIC_L2_BYPASS_AI_ENABLED", True)
    monkeypatch.setattr(funnel_ai_selection, "FUNNEL_THEME_RADAR_PROMOTE_CAP", 2)
    selected = ["000001"]
    trend = ["000001"]
    accum: list[str] = []

    bypass_added, strategic_added, theme_added, mainline_added = funnel_ai_selection.promote_review_candidates(
        selected,
        trend,
        accum,
        {
            "l2_bypass": ["000002"],
            "strategic_l2_bypass": ["000003"],
            "strategic_accum": {"000003"},
            "formal_hit": {"000004"},
            "mainline": ["000005"],
        },
        code_to_total_score={"000002": 2.0, "000003": 3.0, "000004": 4.0, "000005": 5.0},
        code_to_trigger_keys={"000002": ["sos"], "000003": ["spring"], "000004": ["sos"], "000005": ["mainline"]},
        score_map={"000001": 1.0},
        ai_policy={"total_cap": 4},
        use_full_ai_selection=False,
        theme_bonus_map={"000004": 10.0},
        regime="NEUTRAL",
    )

    assert (bypass_added, strategic_added, theme_added, mainline_added) == (0, 0, 1, 1)
    assert selected == ["000001", "000004", "000005"]
    assert trend == ["000001", "000004", "000005"]
    assert accum == []


def test_promote_review_candidates_applies_capital_migration_score_map():
    selected = ["000001"]
    trend = ["000001"]
    accum: list[str] = []
    score_map = {"000001": 10.0}

    funnel_ai_selection.promote_review_candidates(
        selected,
        trend,
        accum,
        {
            "l2_bypass": [],
            "strategic_l2_bypass": [],
            "strategic_accum": set(),
            "formal_hit": set(),
            "mainline": [],
        },
        code_to_total_score={"000001": 10.0},
        code_to_trigger_keys={"000001": ["sos"]},
        score_map=score_map,
        ai_policy={"total_cap": 4},
        use_full_ai_selection=False,
        theme_bonus_map={},
        regime="RISK_ON",
        capital_migration_bonus_map={"000001": 4.5},
    )

    assert score_map["000001"] == 14.5


def test_loss_guard_risk_on_bans_pure_trend_pullback():
    kept, trend_kept, accum_kept, dropped = apply_loss_guard(
        ["000001"],
        ["000001"],
        [],
        regime="RISK_ON",
        code_to_trigger_keys={"000001": ["trend_pullback"]},
        code_to_total_score={"000001": 18.0},
        channel_map={"000001": "趋势延续"},
        df_map={},
    )

    assert kept == []
    assert trend_kept == []
    assert accum_kept == []
    assert dropped == {"单TrendPB仅观察": 1}


def test_loss_guard_blocks_pure_evr_as_observation_only():
    kept, trend_kept, accum_kept, dropped = apply_loss_guard(
        ["000001"],
        ["000001"],
        [],
        regime="NEUTRAL",
        code_to_trigger_keys={"000001": ["evr"]},
        code_to_total_score={"000001": 8.0},
        channel_map={"000001": "吸筹通道"},
        df_map={},
    )

    assert kept == []
    assert trend_kept == []
    assert accum_kept == []
    assert dropped == {"单EVR仅观察": 1}


def test_signal_report_fields_fallback_for_strategic_review():
    fields = signal_report_fields("000001", {}, "Trend", "crash", 0.0)

    assert fields["primary_signal"] == "strategic_review"
    assert fields["signal_types"] == []


def test_signal_report_fields_fallback_prefers_selection_source():
    fields = signal_report_fields("000001", {}, "Trend", "crash", 0.0, "l2_bypass")

    assert fields["primary_signal"] == "l2_bypass"


def test_signal_report_fields_fallback_empty_without_track_or_source():
    fields = signal_report_fields("000001", {}, "", "crash", 0.0)

    assert fields["primary_signal"] == ""
