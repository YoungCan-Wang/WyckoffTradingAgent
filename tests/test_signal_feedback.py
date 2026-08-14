from __future__ import annotations

import argparse

import pandas as pd
import pytest

from core.dynamic_policy import (
    DynamicPolicyConfig,
    build_signal_weight_map,
    filter_triggers_by_registry,
    merge_signal_weight_maps,
    resolve_dynamic_candidate_policy,
)
from core.price_action_footprint import compute_price_action_footprint
from core.signal_confirmation import score_springboard_abc
from core.signal_feedback import build_signal_observations, build_signal_registry_updates, summarize_signal_health
from workflows.dynamic_policy_config import dynamic_policy_config_from_env
from workflows.signal_feedback_job import _outcome_rows, default_registry_horizon


class _FailingUpsertQuery:
    def upsert(self, _rows: list[dict], *, on_conflict: str):
        return self

    def execute(self):
        raise RuntimeError("db down")


class _FailingUpsertClient:
    def table(self, _name: str):
        return _FailingUpsertQuery()


class _CapturingUpsertQuery:
    def __init__(self, client):
        self.client = client

    def upsert(self, rows: list[dict], *, on_conflict: str):
        self.client.rows = rows
        self.client.conflict = on_conflict
        return self

    def execute(self):
        return None


class _CapturingUpsertClient:
    def __init__(self):
        self.rows: list[dict] = []
        self.conflict = ""

    def table(self, name: str):
        self.table_name = name
        return _CapturingUpsertQuery(self)


class _SchemaMissThenCaptureQuery:
    def __init__(self, client):
        self.client = client

    def upsert(self, rows: list[dict], *, on_conflict: str):
        self.client.calls += 1
        self.client.rows = rows
        self.client.conflict = on_conflict
        return self

    def execute(self):
        if self.client.calls == 1:
            raise RuntimeError("Could not find column features_json in schema cache")
        return None


class _SchemaMissThenCaptureClient:
    def __init__(self):
        self.calls = 0
        self.rows: list[dict] = []
        self.conflict = ""

    def table(self, name: str):
        self.table_name = name
        return _SchemaMissThenCaptureQuery(self)


def test_build_signal_observations_marks_selection_and_source():
    rows = build_signal_observations(
        "2026-05-25",
        {"sos": [("000001", 12.5)], "spring": [("000002", 9.0)]},
        regime="risk_on",
        selected_for_ai=["000001"],
        ai_recommended=["000001"],
        name_map={"000001": "平安银行"},
        sector_map={"000001": "银行"},
        score_map={"000001": 88},
        latest_close_map={"000001": 10.5},
        source_map={"000002": "l2_bypass"},
        selection_mode="tradeable_l4",
        selection_mode_map={"000002": "l2_bypass_shadow"},
        footprint_map={
            "sos:000001": {
                "version": "price_action_footprint_v1",
                "bias": "demand",
                "tags": ["quality_breakout"],
                "negative_tags": [],
            }
        },
        springboard_map={
            "sos:000001": {
                "springboard_grade": "A+B",
                "springboard_met_count": 2,
                "springboard_a": True,
                "springboard_b": True,
                "springboard_c": False,
                "springboard_support": 10.1,
                "springboard_touch_count": 1,
                "springboard_evidence": {"a_hits": [{"date": "2026-05-24"}]},
            },
            "spring:000002": {
                "springboard_grade": "C",
                "springboard_met_count": 1,
                "springboard_a": False,
                "springboard_b": False,
                "springboard_c": True,
                "springboard_support": 8.8,
                "springboard_touch_count": 3,
                "springboard_evidence": {"c_support": {"touch_dates": ["2026-05-20"]}},
            },
        },
        source_context_map={
            "000001": {
                "version": "external_capital_context_v1",
                "lhb": {"net_buy": 123.0},
                "margin": {"margin_balance": 456.0},
                "source_status": {"lhb": "ok rows=10 matches=1", "margin_sse": "ok rows=20 matches=1"},
            }
        },
        entry_quality_map={
            "000001": {
                "score": 82.3,
                "grade": "S",
                "tag": "入场质量S(82.3)",
                "risk_flags": "缩量不足、追高延展",
                "priority_bucket": 17,
            }
        },
    )

    first = rows[0]
    second = rows[1]
    assert first["signal_type"] == "sos"
    assert first["track"] == "Trend"
    assert first["selected_for_ai"] is True
    assert first["ai_recommended"] is True
    assert first["entry_price"] == 10.5
    assert first["springboard_grade"] == "A+B"
    assert first["springboard_met_count"] == 2
    assert first["springboard_a"] is True
    assert first["springboard_evidence"]["a_hits"][0]["date"] == "2026-05-24"
    assert first["features_json"]["price_action_footprint"]["tags"] == ["quality_breakout"]
    assert first["features_json"]["springboard"]["springboard_grade"] == "A+B"
    assert first["features_json"]["source_context"]["lhb"]["net_buy"] == 123.0
    assert first["features_json"]["source_context"]["margin"]["margin_balance"] == 456.0
    assert first["features_json"]["entry_quality"] == {
        "version": "step3_entry_quality_v1",
        "score": 82.3,
        "grade": "S",
        "tag": "入场质量S(82.3)",
        "risk_flags": ["缩量不足", "追高延展"],
        "priority_bucket": 17,
    }
    lineage = first["features_json"]["data_lineage"]
    assert lineage["version"] == "candidate_evidence_lineage_v1"
    assert lineage["coverage_score"] == 95.0
    assert lineage["coverage_grade"] == "strong"
    assert lineage["evidence_keys"] == [
        "daily_signal",
        "price_action",
        "springboard",
        "external_capital",
        "ai_review",
    ]
    assert lineage["sources"]["external_capital"]["providers"] == ["lhb", "margin"]
    assert lineage["sources"]["selection"]["candidate_rank"] == 1
    shadow_score = first["features_json"]["candidate_shadow_score"]
    assert shadow_score["version"] == "candidate_shadow_score_v3"
    assert shadow_score["components"]["funnel"] == 26.4
    assert shadow_score["components"]["springboard"] == 12.0
    assert "springboard_structure_ready" in shadow_score["positive_tags"]
    assert second["track"] == "Accum"
    assert second["source"] == "l2_bypass"
    assert second["selection_mode"] == "l2_bypass_shadow"
    assert second["springboard_grade"] == "C"
    assert second["springboard_c"] is True
    assert second["features_json"]["data_lineage"]["coverage_grade"] == "thin"
    assert second["features_json"]["data_lineage"]["missing_keys"] == [
        "price_action",
        "external_capital",
    ]


def test_build_signal_observations_writes_candidate_metadata():
    rows = build_signal_observations(
        "2026-06-25",
        {"mainline": [("300308", 88.0)]},
        selected_for_ai=["300308"],
        ai_recommended=["300308"],
        name_map={"300308": "中际旭创"},
        latest_close_map={"300308": 100.0},
        candidate_metadata_map={
            "300308": {
                "strategy_version": "candidate_lane_v1",
                "candidate_lane": "mainline",
                "entry_type": "主线平台再突破",
                "signal_key": "mainline",
                "candidate_status": "主线买点候选",
                "mainline_score": 0.86,
                "timing_score": 0.72,
            }
        },
    )

    row = rows[0]
    assert row["strategy_version"] == "candidate_lane_v1"
    assert row["candidate_lane"] == "mainline"
    assert row["entry_type"] == "主线平台再突破"
    assert row["candidate_status"] == "主线买点候选"
    assert row["features_json"]["candidate_metadata"]["mainline_score"] == 0.86
    assert row["features_json"]["candidate_metadata"]["timing_score"] == 0.72


def test_daily_job_marks_bypass_observations_as_shadow(monkeypatch):
    from workflows import daily_signal_observations

    monkeypatch.setenv("FUNNEL_AI_SELECTION_MODE", "tradeable_l4")
    rows = daily_signal_observations.build_signal_observation_rows(
        {
            "selected_for_ai": ["000001"],
            "l2_bypass_pool": ["000002"],
            "strategic_l2_bypass_pool": ["000003"],
            "review_triggers": {
                "sos": [("000001", 6.0), ("000002", 5.0)],
                "spring": [("000003", 2.0)],
            },
            "name_map": {"000001": "Alpha", "000002": "Beta", "000003": "Gamma"},
            "metrics": {
                "layer2_channel_map": {"000001": "点火破局"},
                "latest_close_map": {"000001": 10.0, "000002": 8.0, "000003": 6.0},
            },
        },
        "RISK_ON",
        [],
        trade_date="2026-06-24",
    )

    by_code = {row["code"]: row for row in rows}
    assert by_code["000001"]["selection_mode"] == "tradeable_l4"
    assert by_code["000001"]["source"] == "funnel"
    assert by_code["000002"]["selection_mode"] == "l2_bypass_shadow"
    assert by_code["000002"]["source"] == "l2_bypass_shadow"
    assert by_code["000003"]["selection_mode"] == "strategic_l2_bypass_shadow"
    assert by_code["000003"]["source"] == "strategic_l2_bypass_shadow"


def test_daily_job_signal_observations_attach_entry_quality():
    from workflows import daily_signal_observations

    rows = daily_signal_observations.build_signal_observation_rows(
        {
            "selected_for_ai": ["000001"],
            "review_triggers": {"sos": [("000001", 6.0)]},
            "candidate_entries": [
                {
                    "code": "000001",
                    "priority_score": 88.0,
                    "track": "trend",
                    "entry_type": "sos",
                    "rs_10": 8.0,
                    "min_vol_ratio_5d": 0.6,
                    "bias_200": 10.0,
                    "avg_amount_20_yi": 3.0,
                }
            ],
        },
        "RISK_ON",
        [],
        trade_date="2026-06-24",
    )

    entry_quality = rows[0]["features_json"]["entry_quality"]
    assert entry_quality["version"] == "step3_entry_quality_v1"
    assert entry_quality["grade"] == "S"
    assert entry_quality["score"] >= 80


def test_external_capital_context_normalizes_sources():
    from integrations.external_capital_context import build_external_capital_context

    class FakeAk:
        def stock_lhb_detail_em(self, *, start_date: str, end_date: str):
            assert start_date == "20260612"
            assert end_date == "20260612"
            return pd.DataFrame([{"代码": "000001", "龙虎榜净买额": 1200, "解读": "机构净买"}])

        def stock_margin_detail_sse(self, *, date: str):
            assert date == "20260612"
            return pd.DataFrame([{"标的证券代码": "600000", "融资余额": 9000, "融资买入额": 300}])

        def stock_margin_detail_szse(self, *, date: str):
            assert date == "20260612"
            return pd.DataFrame([{"标的证券代码": "000001", "融资余额": 8000, "融资买入额": 200}])

        def stock_dzjy_mrmx(self, *, symbol: str, start_date: str, end_date: str):
            assert symbol == "A股"
            assert start_date == "20260612"
            assert end_date == "20260612"
            return pd.DataFrame(
                [
                    {"证券代码": "000001", "成交额": 500.0, "折溢率": -2.5, "买方营业部": "买方A"},
                    {"证券代码": "000001", "成交额": 300.0, "折溢率": -1.5, "买方营业部": "买方B"},
                ]
            )

        def stock_zh_a_tick_tx_js(self, *, symbol: str):
            assert symbol == "sz000001"
            return pd.DataFrame(
                [
                    {"成交时间": "09:30:00", "成交价格": 10.1, "成交金额": 2_000_000, "性质": "买盘"},
                    {"成交时间": "09:31:00", "成交价格": 10.0, "成交金额": 1_500_000, "性质": "卖盘"},
                    {"成交时间": "09:32:00", "成交价格": 10.0, "成交金额": 200_000, "性质": "买盘"},
                ]
            )

    got = build_external_capital_context(
        ["000001", "600000"],
        "2026-06-12",
        include_tick=True,
        tick_max_symbols=1,
        tick_min_amount_yuan=1_000_000,
        ak_module=FakeAk(),
    )

    assert got["000001"]["lhb"]["net_buy"] == 1200
    assert got["000001"]["margin"]["margin_balance"] == 8000
    assert got["000001"]["block_trade"]["trade_count"] == 2
    assert got["000001"]["tick_large_order"]["large_net_amount_yuan"] == 500_000
    assert got["600000"]["margin"]["margin_buy"] == 300
    assert "tick_large_order" not in got["600000"]


def test_daily_job_builds_external_capital_context_map(monkeypatch):
    from integrations import external_capital_context
    from workflows import daily_signal_observations

    captured = {}

    def fake_build(codes, trade_date, *, include_tick, tick_max_symbols, tick_min_amount_yuan):
        captured.update(
            {
                "codes": codes,
                "trade_date": trade_date,
                "include_tick": include_tick,
                "tick_max_symbols": tick_max_symbols,
                "tick_min_amount_yuan": tick_min_amount_yuan,
            }
        )
        return {"000001": {"version": "external_capital_context_v1", "margin": {"margin_balance": 1}}}

    monkeypatch.setenv("FUNNEL_EXTERNAL_CAPITAL_CONTEXT", "1")
    monkeypatch.setenv("FUNNEL_EXTERNAL_CAPITAL_MAX_SYMBOLS", "1")
    monkeypatch.setenv("FUNNEL_EXTERNAL_CAPITAL_TICK_CONTEXT", "0")
    monkeypatch.setattr(external_capital_context, "build_external_capital_context", fake_build)

    got = daily_signal_observations.build_external_capital_context_map(
        {
            "selected_for_ai": ["000001", "000002"],
            "review_triggers": {"sos": [("000001", 6.0), ("000002", 5.0)]},
        },
        [],
        None,
        trade_date="2026-06-12",
    )

    assert captured["codes"] == ["000001"]
    assert captured["trade_date"] == "2026-06-12"
    assert captured["include_tick"] is False
    assert captured["tick_max_symbols"] == 3
    assert captured["tick_min_amount_yuan"] == 1_000_000
    assert got["000001"]["margin"]["margin_balance"] == 1


def test_external_capital_context_falls_back_to_formal_observation_codes(monkeypatch):
    from integrations import external_capital_context
    from workflows import daily_signal_observations

    captured = {}

    def fake_build(codes, _trade_date, **_kwargs):
        captured["codes"] = codes
        return {code: {"stock_moneyflow": {"net_amount_wan": 1}} for code in codes}

    monkeypatch.setattr(external_capital_context, "build_external_capital_context", fake_build)
    got = daily_signal_observations.build_external_capital_context_map(
        {"formal_triggers": {"sos": [("000001", 5.0), ("000002", 8.0)]}},
        [],
        None,
        trade_date="2026-06-12",
    )

    assert captured["codes"] == ["000002", "000001"]
    assert set(got) == {"000001", "000002"}


def test_price_action_footprint_marks_breakout_and_supply_pressure():
    dates = pd.date_range("2026-05-01", periods=30, freq="D")
    df = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * 30,
            "high": [10.4] * 29 + [11.2],
            "low": [9.8] * 30,
            "close": [10.1] * 29 + [9.95],
            "volume": [100.0] * 29 + [260.0],
        }
    )

    fp = compute_price_action_footprint(df, "sos")

    assert fp["failed_breakout_20"] is True
    assert "failed_breakout" in fp["negative_tags"]
    assert fp["supply_pressure_score"] >= 70


def test_score_springboard_abc_returns_persistable_metadata():
    dates = pd.date_range("2026-05-01", periods=25, freq="D")
    df = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * 25,
            "high": [11.0] * 25,
            "low": [10.0] * 25,
            "close": [10.5] * 25,
            "volume": [100.0] * 25,
        }
    )
    df.loc[22, ["close", "volume"]] = [10.8, 50.0]
    df.loc[24, ["close", "volume"]] = [10.9, 220.0]

    result = score_springboard_abc(df, "spring")

    assert result["a"] is True
    assert result["b"] is True
    assert result["c"] is True
    assert result["grade"] == "A+B+C"
    assert result["touch_count"] >= 2
    assert result["evidence"]["b_last"]["date"] == "2026-05-25"


def test_signal_feedback_upsert_errors_propagate(monkeypatch):
    from integrations import supabase_signal_feedback

    closed = []
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(supabase_signal_feedback, "_configured", lambda: True)
    monkeypatch.setattr(supabase_signal_feedback, "_admin", _FailingUpsertClient)
    monkeypatch.setattr(supabase_signal_feedback, "_close", closed.append)

    with pytest.raises(RuntimeError, match="db down"):
        supabase_signal_feedback.upsert_signal_outcomes([{"observation_id": 1, "horizon_days": 1}])

    assert len(closed) == 1


def test_signal_feedback_upsert_rejects_cli_context(monkeypatch):
    from integrations import supabase_signal_feedback

    monkeypatch.delenv("WYCKOFF_WRITE_CONTEXT", raising=False)
    monkeypatch.setattr(supabase_signal_feedback, "_configured", lambda: True)

    with pytest.raises(PermissionError, match="server_job"):
        supabase_signal_feedback.upsert_signal_outcomes([{"observation_id": 1, "horizon_days": 1}])


def test_signal_observations_conflict_keeps_daily_tags(monkeypatch):
    from integrations import supabase_signal_feedback

    client = _CapturingUpsertClient()
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(supabase_signal_feedback, "_configured", lambda: True)
    monkeypatch.setattr(supabase_signal_feedback, "_admin", lambda: client)
    monkeypatch.setattr(supabase_signal_feedback, "_close", lambda _client: None)

    rows = [
        {"market": "cn", "trade_date": "2026-06-10", "code": "000001", "signal_type": "spring"},
        {"market": "cn", "trade_date": "2026-06-11", "code": "000001", "signal_type": "lps"},
    ]

    assert supabase_signal_feedback.upsert_signal_observations(rows) == 2
    assert client.conflict == "market,trade_date,code,signal_type"
    assert [row["trade_date"] for row in client.rows] == ["2026-06-10", "2026-06-11"]


def test_signal_observations_drop_features_json_when_schema_missing(monkeypatch):
    from integrations import supabase_signal_feedback

    client = _SchemaMissThenCaptureClient()
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(supabase_signal_feedback, "_configured", lambda: True)
    monkeypatch.setattr(supabase_signal_feedback, "_admin", lambda: client)
    monkeypatch.setattr(supabase_signal_feedback, "_close", lambda _client: None)

    rows = [
        {
            "market": "cn",
            "trade_date": "2026-06-10",
            "code": "000001",
            "signal_type": "spring",
            "features_json": {"price_action_footprint": {"bias": "demand"}},
        }
    ]

    assert supabase_signal_feedback.upsert_signal_observations(rows) == 1
    assert client.calls == 2
    assert "features_json" not in client.rows[0]


def test_summarize_signal_health_classifies_watch_and_all_regime():
    outcomes = []
    for idx in range(20):
        outcomes.append(
            {
                "signal_type": "spring",
                "track": "Accum",
                "regime": "RISK_OFF",
                "horizon_days": 10,
                "status": "done",
                "return_pct": -1 if idx < 14 else 2,
                "max_drawdown_pct": -3,
            }
        )

    rows = summarize_signal_health(outcomes, as_of_date="2026-05-25", min_samples=20)
    by_regime = {row["regime"]: row for row in rows}

    assert set(by_regime) == {"ALL", "RISK_OFF"}
    assert by_regime["ALL"]["health_state"] == "DECAYED"
    assert by_regime["ALL"]["weight_multiplier"] == 0.4
    assert by_regime["RISK_OFF"]["sample_count"] == 20


def test_dynamic_policy_shifts_quota_toward_healthier_track():
    base = {
        "quota_family": "NEUTRAL",
        "total_cap": 10,
        "requested_trend_quota": 5,
        "requested_accum_quota": 5,
        "trend_quota": 5,
        "accum_quota": 5,
    }

    policy = resolve_dynamic_candidate_policy(base, {"sos": 1.0, "spring": 0.4})

    assert policy["quota_family"] == "NEUTRAL+DYNAMIC"
    assert policy["trend_quota"] > policy["accum_quota"]


def test_dynamic_policy_tracks_scoped_weight_by_base_signal():
    base = {
        "quota_family": "NEUTRAL",
        "total_cap": 10,
        "requested_trend_quota": 5,
        "requested_accum_quota": 5,
        "trend_quota": 5,
        "accum_quota": 5,
    }

    policy = resolve_dynamic_candidate_policy(
        base,
        {"sos|regime=RISK_ON": 1.0, "lps|regime=RISK_ON|lane=trend_pullback": 0.4},
    )

    assert policy["quota_family"] == "NEUTRAL+DYNAMIC"
    assert policy["trend_quota"] > policy["accum_quota"]


def test_dynamic_policy_scoped_weight_does_not_double_count_same_signal():
    """同一信号的全局 key 与 regime-scoped key 不应被当成两个样本重复计入轨道均值。"""
    base = {
        "quota_family": "NEUTRAL",
        "total_cap": 10,
        "requested_trend_quota": 5,
        "requested_accum_quota": 5,
        "trend_quota": 5,
        "accum_quota": 5,
    }
    # sos 的全局权重是 1.0（健康），但 RISK_ON 下的精确评估是 0.4（已衰退）。
    # 若重复计入均值，会把 Trend 权重错误算成 (1.0+0.4)/2=0.7；
    # 正确做法是 scoped key 更精确，应完全覆盖全局值，得到 0.4。
    policy = resolve_dynamic_candidate_policy(base, {"sos": 1.0, "sos|regime=RISK_ON": 0.4})

    assert policy["trend_health_weight"] == 0.4


def test_dynamic_policy_uses_configured_feedback_horizon():
    weights = build_signal_weight_map(
        [
            {"as_of_date": "2026-06-10", "horizon_days": 10, "signal_type": "lps", "weight_multiplier": 1.2},
            {"as_of_date": "2026-06-10", "horizon_days": 5, "signal_type": "lps", "weight_multiplier": 0.4},
        ],
        config=DynamicPolicyConfig(horizon_days=5),
    )

    assert weights["lps"] == 0.4


def test_dynamic_policy_merges_attribution_weights_conservatively():
    weights = merge_signal_weight_maps(
        {"lps": 0.75, "sos": 1.1},
        {"lps": 0.5, "sos": 1.15, "evr": 0.75},
    )

    assert weights == {"evr": 0.75, "lps": 0.5, "sos": 1.15}


def test_dynamic_policy_env_loader_stays_in_workflow_layer(monkeypatch):
    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "shadow")
    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY_HORIZON", "8")

    config = dynamic_policy_config_from_env()

    assert config.normalized_mode() == "shadow"
    assert config.normalized_horizon() == 8


def test_signal_feedback_registry_horizon_defaults_to_five(monkeypatch):
    monkeypatch.delenv("SIGNAL_REGISTRY_HORIZON", raising=False)

    assert default_registry_horizon() == 5


def test_registry_retires_after_repeated_decay():
    updates = build_signal_registry_updates(
        [
            {
                "signal_type": "spring",
                "track": "Accum",
                "regime": "ALL",
                "horizon_days": 10,
                "health_state": "DECAYED",
                "weight_multiplier": 0.4,
            }
        ],
        registry_rows=[{"signal_type": "spring", "status": "WATCH"}],
    )

    assert updates[0]["status"] == "RETIRED"


def test_registry_updates_preserve_retired_rows_missing_from_health_window():
    updates = build_signal_registry_updates(
        [
            {
                "signal_type": "sos",
                "track": "Trend",
                "regime": "ALL",
                "horizon_days": 5,
                "health_state": "HEALTHY",
                "weight_multiplier": 1.0,
            }
        ],
        market="cn",
        horizon_days=5,
        registry_rows=[
            {"signal_type": "sos", "regime": "", "status": "ACTIVE", "weight_multiplier": 1.0},
            {
                "signal_type": "spring",
                "regime": "",
                "status": "RETIRED",
                "weight_multiplier": 0.0,
                "track": "Accum",
                "reason": "repeated decay",
            },
            {
                "signal_type": "launchpad",
                "regime": "RISK_ON",
                "status": "ACTIVE",
                "weight_multiplier": 0.3,
                "track": "Accum",
            },
        ],
    )

    by_key = {(row["signal_type"], row.get("regime") or ""): row for row in updates}
    assert by_key[("sos", "")]["status"] == "ACTIVE"
    assert by_key[("spring", "")]["status"] == "RETIRED"
    assert by_key[("launchpad", "RISK_ON")]["weight_multiplier"] == 0.3
    assert filter_triggers_by_registry(
        {"sos": [("000001", 1.0)], "spring": [("000002", 1.0)]},
        updates,
    ) == {"sos": [("000001", 1.0)]}


def test_registry_regime_rows_follow_global_retired_status():
    updates = build_signal_registry_updates(
        [
            {
                "signal_type": "spring",
                "track": "Accum",
                "regime": "ALL",
                "horizon_days": 5,
                "health_state": "DECAYED",
                "weight_multiplier": 0.4,
            },
            {
                "signal_type": "spring",
                "track": "Accum",
                "regime": "RISK_ON",
                "horizon_days": 5,
                "health_state": "HEALTHY",
                "weight_multiplier": 1.0,
            },
        ],
        horizon_days=5,
        registry_rows=[{"signal_type": "spring", "regime": "", "status": "WATCH"}],
    )

    by_key = {(row["signal_type"], row.get("regime") or ""): row for row in updates}
    assert by_key[("spring", "")]["status"] == "RETIRED"
    assert by_key[("spring", "RISK_ON")]["status"] == "RETIRED"
    assert filter_triggers_by_registry({"spring": [("000001", 1.0)]}, updates) == {}


def test_registry_ignores_stale_regime_status_when_global_retired():
    updates = build_signal_registry_updates(
        [
            {
                "signal_type": "spring",
                "track": "Accum",
                "regime": "ALL",
                "horizon_days": 5,
                "health_state": "DECAYED",
                "weight_multiplier": 0.4,
            }
        ],
        horizon_days=5,
        registry_rows=[
            {"signal_type": "spring", "regime": "", "status": "RETIRED", "weight_multiplier": 0.0},
            {
                "signal_type": "spring",
                "regime": "RISK_ON",
                "status": "ACTIVE",
                "weight_multiplier": 0.5,
                "track": "Accum",
            },
        ],
    )

    by_key = {(row["signal_type"], row.get("regime") or ""): row for row in updates}
    assert by_key[("spring", "")]["status"] == "RETIRED"
    assert by_key[("spring", "RISK_ON")]["status"] == "RETIRED"
    assert filter_triggers_by_registry({"spring": [("000001", 1.0)]}, updates) == {}


def test_registry_keeps_retired_on_insufficient_health():
    updates = build_signal_registry_updates(
        [
            {
                "signal_type": "spring",
                "track": "Accum",
                "regime": "ALL",
                "horizon_days": 5,
                "health_state": "INSUFFICIENT",
                "weight_multiplier": 0.6,
            }
        ],
        horizon_days=5,
        registry_rows=[{"signal_type": "spring", "regime": "", "status": "RETIRED"}],
    )

    assert updates[0]["status"] == "RETIRED"
    assert filter_triggers_by_registry({"spring": [("000001", 1.0)]}, updates) == {}


def test_filter_triggers_by_registry_blocks_experimental_signal():
    filtered = filter_triggers_by_registry(
        {"sos": [("000001", 1.0)], "spring": [("000002", 1.0)]},
        [{"signal_type": "spring", "status": "EXPERIMENTAL"}],
    )

    assert "sos" in filtered
    assert "spring" not in filtered


def test_shadow_selection_diff_preserves_shadow_order():
    from workflows.funnel_ai_selection import selection_diff

    added, removed = selection_diff(["000001", "000002"], ["000002", "000003"])

    assert added == ["000003"]
    assert removed == ["000001"]


def test_attach_shadow_policy_preserves_base_policy():
    from workflows.funnel_ai_selection import attach_shadow_policy

    base = {"trend_quota": 8, "accum_quota": 4, "quota_family": "FULL_FORMAL_L4"}
    shadow = {"trend_quota": 3, "accum_quota": 5, "quota_family": "RISK_ON+DYNAMIC"}

    attach_shadow_policy(
        base,
        {
            "mode": "shadow",
            "policy": shadow,
            "weights": {"sos": 0.8},
            "registry": [{"signal_type": "sos"}],
            "health": [{"signal_type": "sos"}],
        },
    )

    assert base["trend_quota"] == 8
    assert base["accum_quota"] == 4
    assert base["_dynamic_mode"] == "shadow"
    assert base["_shadow_policy"] == shadow
    assert base["_signal_weights"] == {"sos": 0.8}
    assert base["_attribution_signal_weights"] == {}


def test_load_dynamic_policy_context_merges_attribution_weights(monkeypatch):
    from core.ai_candidate_allocation import AiCandidateAllocationConfig
    from workflows import funnel_ai_selection as selection
    from workflows.strategy_attribution_policy import AttributionPolicySnapshot

    monkeypatch.setattr(
        selection,
        "load_signal_health_snapshot",
        lambda market: [
            {
                "as_of_date": "2026-07-04",
                "horizon_days": 5,
                "regime": "RISK_ON",
                "signal_type": "lps",
                "weight_multiplier": 0.75,
            }
        ],
    )
    monkeypatch.setattr(selection, "load_signal_registry", lambda market: [])
    monkeypatch.setattr(
        selection,
        "load_attribution_policy_snapshot",
        lambda **_kwargs: AttributionPolicySnapshot(
            weights={"lps": 0.5, "sos": 1.15},
            source="远端",
            report_date="2026-07-04",
            horizon="5",
            age_days=0,
            next_action="manual_review_dynamic_on",
            formal_dynamic_allowed=True,
        ),
    )

    ctx = selection._load_dynamic_policy_context(
        "RISK_ON",
        {"breadth": {}},
        DynamicPolicyConfig(mode="shadow", horizon_days=5),
        AiCandidateAllocationConfig(),
    )

    assert ctx["weights"]["lps"] == 0.5
    assert ctx["weights"]["sos"] == 1.15
    assert ctx["attribution_weights"] == {"lps": 0.5, "sos": 1.15}
    assert ctx["attribution_policy_meta"]["report_date"] == "2026-07-04"
    assert ctx["attribution_policy_meta"]["next_action"] == "manual_review_dynamic_on"


def test_load_dynamic_policy_context_blocks_attribution_weights_in_formal_on(monkeypatch):
    from core.ai_candidate_allocation import AiCandidateAllocationConfig
    from workflows import funnel_ai_selection as selection
    from workflows.strategy_attribution_policy import AttributionPolicySnapshot

    monkeypatch.setattr(selection, "load_signal_health_snapshot", lambda market: [])
    monkeypatch.setattr(selection, "load_signal_registry", lambda market: [])
    monkeypatch.setattr(
        selection,
        "load_attribution_policy_snapshot",
        lambda **_kwargs: AttributionPolicySnapshot(
            weights={"lps": 0.5},
            source="远端",
            report_date="2026-07-04",
            horizon="5",
            age_days=0,
            next_action="keep_static_policy",
            formal_dynamic_allowed=False,
            formal_dynamic_block_reason="next_action=keep_static_policy",
        ),
    )

    ctx = selection._load_dynamic_policy_context(
        "RISK_ON",
        {"breadth": {}},
        DynamicPolicyConfig(mode="on", horizon_days=5),
        AiCandidateAllocationConfig(),
    )

    assert ctx["weights"] == {}
    assert ctx["attribution_weights"] == {}
    assert ctx["attribution_policy_meta"]["weight_count"] == 1
    assert ctx["attribution_policy_meta"]["formal_dynamic_allowed"] is False
    assert ctx["attribution_policy_meta"]["formal_dynamic_block_reason"] == "next_action=keep_static_policy"


def test_policy_shadow_row_stores_compact_summaries():
    from workflows.funnel_ai_selection import _policy_shadow_row

    row = _policy_shadow_row(
        {
            "trend_quota": 8,
            "accum_quota": 4,
            "quota_family": "FULL_FORMAL_L4",
            "_shadow_policy": {"trend_quota": 3, "accum_quota": 5, "quota_family": "RISK_ON+DYNAMIC"},
            "_signal_weights": {"sos": 0.8, "spring": 1.2},
            "_registry_rows": [
                {"signal_type": "sos", "status": "ACTIVE", "weight_multiplier": 1.0},
                {"signal_type": "lps", "status": "WATCH", "weight_multiplier": 0.5, "sample_count": 22},
            ],
            "_health_rows": [
                {
                    "signal_type": "lps",
                    "regime": "RISK_ON",
                    "horizon_days": 5,
                    "health_state": "DECAYED",
                    "weight_multiplier": 0.4,
                    "sample_count": 18,
                    "avg_return_pct": -3.2,
                }
            ],
            "_attribution_signal_weights": {"sos": 0.8},
            "_attribution_policy_meta": {
                "source": "远端",
                "report_date": "2026-07-04",
                "horizon": "5",
                "age_days": 0,
                "next_action": "manual_review_dynamic_on",
                "formal_dynamic_allowed": True,
            },
        },
        {"end_trade_date": "2026-06-30"},
        ["000001", "000002"],
        ["000002", "000003"],
        ["000003"],
        ["000001"],
        "RISK_ON",
    )

    assert row["schema_version"] == "shadow_policy_v2"
    assert row["snapshot_level"] == "summary"
    assert row["attribution_signal_weights"] == {"sos": 0.8}
    assert row["attribution_policy_meta"]["report_date"] == "2026-07-04"
    assert row["selection_summary"]["jaccard"] == 0.3333
    assert row["policy_summary"]["attribution_weight_count"] == 1
    assert row["policy_summary"]["attribution_policy_meta"]["source"] == "远端"
    assert row["policy_summary"]["attribution_policy_meta"]["next_action"] == "manual_review_dynamic_on"
    downweighted = row["policy_summary"]["downweighted_signals"][0]
    assert downweighted["signal_type"] == "sos"
    assert downweighted["weight"] == 0.8
    assert row["registry_summary"]["by_status"] == {"ACTIVE": 1, "WATCH": 1}
    assert row["health_summary"]["changed"][0]["state"] == "DECAYED"
    assert row["registry_snapshot"] == []
    assert row["health_snapshot"] == []


def test_signal_feedback_job_builds_outcome_rows():
    obs = {
        "id": 1,
        "market": "cn",
        "trade_date": "2024-01-02",
        "code": "000001",
        "signal_type": "sos",
        "track": "Trend",
        "regime": "NEUTRAL",
        "entry_price": 11,
    }
    hist = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=4).astype(str),
            "close": [10, 11, 12, 13],
            "low": [9, 10.5, 11.5, 12],
        }
    )

    rows = _outcome_rows(obs, hist, argparse.Namespace(horizons=(1,)).horizons)

    assert rows[0]["observation_id"] == 1
    assert rows[0]["horizon_days"] == 1
    assert rows[0]["status"] == "done"
    assert round(rows[0]["return_pct"], 2) == 9.09


def test_observations_to_settle_backfills_pending_outcomes_outside_window(monkeypatch):
    """滚动窗口外仍卡在 pending 的 observation 应被补进本次结算，而不是永久遗漏。"""
    from workflows import signal_feedback_job as job

    recent = [{"id": 5, "code": "000005"}]
    stale_pending = [{"id": 1, "code": "000001"}]
    monkeypatch.setattr(job, "load_recent_signal_observations", lambda *_a, **_k: recent)
    monkeypatch.setattr(job, "load_pending_outcome_observation_ids", lambda *_a, **_k: [1, 5])
    monkeypatch.setattr(
        job, "load_signal_observations_by_ids", lambda ids, _market: stale_pending if ids == [1] else []
    )

    result = job._observations_to_settle(job.SignalFeedbackConfig())

    assert result == [*recent, *stale_pending]


def test_observations_to_settle_skips_ids_already_in_recent_window(monkeypatch):
    """已在滚动窗口内的 observation 不应重复补拉。"""
    from workflows import signal_feedback_job as job

    recent = [{"id": 5, "code": "000005"}]
    requested_ids: list[list[int]] = []
    monkeypatch.setattr(job, "load_recent_signal_observations", lambda *_a, **_k: recent)
    monkeypatch.setattr(job, "load_pending_outcome_observation_ids", lambda *_a, **_k: [5])
    monkeypatch.setattr(job, "load_signal_observations_by_ids", lambda ids, _market: requested_ids.append(ids) or [])

    result = job._observations_to_settle(job.SignalFeedbackConfig())

    assert result == recent
    assert requested_ids == [[]]


def test_build_signal_weight_map_includes_regime_scoped_keys():
    """build_signal_weight_map 应为 health 行生成带 regime 的 scoped key。"""
    health_rows = [
        {
            "as_of_date": "2026-06-10",
            "signal_type": "launchpad",
            "regime": "RISK_ON",
            "horizon_days": 5,
            "weight_multiplier": 1.0,
        },
    ]
    weights = build_signal_weight_map(health_rows, regime="RISK_ON")
    assert "launchpad" in weights
    assert weights["launchpad"] == 1.0
    assert "launchpad|regime=RISK_ON" in weights
    assert weights["launchpad|regime=RISK_ON"] == 1.0


def test_build_signal_weight_map_regime_scoped_registry_overrides():
    """registry 中带 regime 的精确行应生成 scoped key，且不影响全局权重。"""
    health_rows = [
        {
            "as_of_date": "2026-06-10",
            "signal_type": "launchpad",
            "regime": "ALL",
            "horizon_days": 5,
            "weight_multiplier": 0.75,
        },
    ]
    registry_rows = [
        {"signal_type": "launchpad", "status": "WATCH", "weight_multiplier": 0.75, "regime": ""},
        {"signal_type": "launchpad", "status": "ACTIVE", "weight_multiplier": 1.0, "regime": "RISK_ON"},
        {"signal_type": "launchpad", "status": "ACTIVE", "weight_multiplier": 0.4, "regime": "PANIC_REPAIR"},
    ]
    weights = build_signal_weight_map(health_rows, registry_rows, regime="RISK_ON")
    # 全局权重由 health + 全局 registry 行决定
    assert weights["launchpad"] == 0.75
    # RISK_ON 精确行不应覆盖全局权重
    assert weights.get("launchpad|regime=RISK_ON") == 1.0
    # PANIC_REPAIR 精确行也应有 scoped key
    assert weights.get("launchpad|regime=PANIC_REPAIR") == 0.4


def test_build_signal_weight_map_regime_scoped_health_overrides_global():
    """health 行中精确匹配当前 regime 的行应优先于全局行。"""
    health_rows = [
        {
            "as_of_date": "2026-06-10",
            "signal_type": "launchpad",
            "regime": "ALL",
            "horizon_days": 5,
            "weight_multiplier": 0.75,
        },
        {
            "as_of_date": "2026-06-10",
            "signal_type": "launchpad",
            "regime": "RISK_ON",
            "horizon_days": 5,
            "weight_multiplier": 1.0,
        },
    ]
    weights = build_signal_weight_map(health_rows, regime="RISK_ON")
    # 精确匹配 RISK_ON 的 health 行应覆盖全局行
    assert weights["launchpad"] == 1.0
    assert weights.get("launchpad|regime=RISK_ON") == 1.0


def test_build_signal_weight_map_missing_multiplier_defaults_to_no_adjustment():
    """health/registry 行缺失 weight_multiplier 时应兜底 1.0（不调权），不能误伤为 0.0（完全禁用）。"""
    health_rows = [
        {"as_of_date": "2026-06-10", "signal_type": "sos", "regime": "ALL", "horizon_days": 5},
    ]
    registry_rows = [
        {"signal_type": "evr", "status": "WATCH", "regime": ""},
    ]
    weights = build_signal_weight_map(health_rows, registry_rows, regime="NEUTRAL")
    assert weights["sos"] == 1.0
    assert weights["evr"] == 1.0


def test_build_signal_registry_updates_includes_regime_rows():
    """build_signal_registry_updates 应同时输出全局行和按 regime 拆分的行。"""
    health_rows = [
        {
            "regime": "ALL",
            "horizon_days": 5,
            "signal_type": "launchpad",
            "track": "Trend",
            "health_state": "WATCH",
            "weight_multiplier": 0.75,
            "sample_count": 370,
            "win_rate_pct": 41.4,
            "avg_return_pct": -0.86,
            "reason": "win=41.4%, avg=-0.86%",
        },
        {
            "regime": "RISK_ON",
            "horizon_days": 5,
            "signal_type": "launchpad",
            "track": "Trend",
            "health_state": "HEALTHY",
            "weight_multiplier": 1.0,
            "sample_count": 64,
            "win_rate_pct": 67.2,
            "avg_return_pct": 6.61,
            "reason": "win=67.2%, avg=+6.61%",
        },
    ]
    updates = build_signal_registry_updates(health_rows, market="cn", horizon_days=5)
    regimes = {row["regime"] for row in updates}
    assert "" in regimes  # 全局行 regime 为空字符串
    assert "RISK_ON" in regimes
    regime_row = next(r for r in updates if r["regime"] == "RISK_ON")
    assert regime_row["weight_multiplier"] == 1.0


def test_fetch_history_returns_empty_on_delisted_stock(monkeypatch):
    """退市/停牌股 fetch_stock_hist 抛出 RuntimeError 时，_fetch_history 应返回空 DataFrame 而非崩溃。"""
    from integrations import data_source
    from workflows import signal_feedback_job as job

    def _boom(symbol, start, end, adjust="qfq"):
        raise RuntimeError(f"数据拉取全线失败 [标:{symbol}]")

    monkeypatch.setattr(data_source, "fetch_stock_hist", _boom)

    obs = {"market": "cn", "code": "920367", "trade_date": "2024-01-15"}
    result = job._fetch_history(obs, "2024-03-01", 10)

    assert result.empty


def test_refresh_outcomes_skips_delisted_without_crash(monkeypatch):
    """refresh_outcomes 遇到退市股应跳过并正常结算剩余标的。"""
    from integrations import data_source
    from workflows import signal_feedback_job as job

    good_hist = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=4).astype(str),
            "close": [10, 11, 12, 13],
            "low": [9, 10.5, 11.5, 12],
        }
    )
    fetch_calls = 0

    def _fetch_side_effect(symbol, start, end, adjust="qfq"):
        nonlocal fetch_calls
        fetch_calls += 1
        if symbol == "920367":
            raise RuntimeError("数据拉取全线失败 [标:920367]")
        return good_hist

    good_obs = {
        "id": 1,
        "market": "cn",
        "trade_date": "2024-01-02",
        "code": "000001",
        "signal_type": "sos",
        "track": "Trend",
        "regime": "NEUTRAL",
        "entry_price": 11,
    }
    bad_obs = {
        "id": 2,
        "market": "cn",
        "trade_date": "2024-01-02",
        "code": "920367",
        "signal_type": "spring",
        "track": "Accum",
        "regime": "NEUTRAL",
        "entry_price": 5,
    }

    monkeypatch.setattr(data_source, "fetch_stock_hist", _fetch_side_effect)
    monkeypatch.setattr(job, "load_recent_signal_observations", lambda *_a, **_k: [good_obs, bad_obs])
    monkeypatch.setattr(job, "load_pending_outcome_observation_ids", lambda *_a, **_k: [])
    monkeypatch.setattr(job, "load_signal_outcome_states", lambda *_a, **_k: {})
    monkeypatch.setattr(job, "upsert_signal_outcomes", lambda rows: len(rows))

    log_lines: list[str] = []
    written = job.refresh_outcomes(job.SignalFeedbackConfig(end_date="2024-03-01"), log_fn=log_lines.append)

    assert written > 0
    assert any("skipped=1" in line for line in log_lines)


def test_refresh_outcomes_fetches_once_per_symbol_and_only_unsettled_horizons(monkeypatch):
    from integrations import data_source
    from workflows import signal_feedback_job as job

    observations = [
        {
            "id": 1,
            "market": "cn",
            "trade_date": "2024-01-02",
            "code": "000001",
            "signal_type": "sos",
            "entry_price": 10,
        },
        {
            "id": 2,
            "market": "cn",
            "trade_date": "2024-01-03",
            "code": "000001",
            "signal_type": "spring",
            "entry_price": 11,
        },
    ]
    history = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=8).astype(str),
            "close": [9, 10, 11, 12, 13, 14, 15, 16],
            "low": [8.5, 9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5],
        }
    )
    fetches: list[tuple[str, str, str]] = []
    written: list[dict] = []

    def fake_fetch(symbol, start, end, adjust="qfq"):
        fetches.append((symbol, start, end))
        return history

    monkeypatch.setattr(data_source, "fetch_stock_hist", fake_fetch)
    monkeypatch.setattr(job, "load_recent_signal_observations", lambda *_a, **_k: observations)
    monkeypatch.setattr(job, "load_pending_outcome_observation_ids", lambda *_a, **_k: [])
    monkeypatch.setattr(
        job,
        "load_signal_outcome_states",
        lambda *_a, **_k: {1: {1: "done", 3: "pending"}, 2: {1: "done"}},
    )
    monkeypatch.setattr(job, "upsert_signal_outcomes", lambda rows: written.extend(rows) or len(rows))

    result = job.refresh_outcomes(
        job.SignalFeedbackConfig(end_date="2024-02-01", horizons=(1, 3)),
        log_fn=lambda _line: None,
    )

    assert result == 2
    assert len(fetches) == 1
    assert {(row["observation_id"], row["horizon_days"]) for row in written} == {(1, 3), (2, 3)}


def test_refresh_outcomes_skips_symbols_when_every_horizon_is_done(monkeypatch):
    from integrations import data_source
    from workflows import signal_feedback_job as job

    observation = {"id": 1, "market": "cn", "trade_date": "2024-01-02", "code": "000001"}
    monkeypatch.setattr(job, "load_recent_signal_observations", lambda *_a, **_k: [observation])
    monkeypatch.setattr(job, "load_pending_outcome_observation_ids", lambda *_a, **_k: [])
    monkeypatch.setattr(job, "load_signal_outcome_states", lambda *_a, **_k: {1: {1: "done", 3: "done"}})
    monkeypatch.setattr(data_source, "fetch_stock_hist", lambda *_a, **_k: pytest.fail("history should not be fetched"))
    monkeypatch.setattr(job, "upsert_signal_outcomes", lambda rows: len(rows))

    result = job.refresh_outcomes(job.SignalFeedbackConfig(horizons=(1, 3)), log_fn=lambda _line: None)

    assert result == 0
