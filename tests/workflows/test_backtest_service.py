from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from workflows.backtest import BacktestWorkflowRequest, run_backtest_request
from workflows.backtest_data import BacktestHistory, BacktestMetadata, BacktestUniverse
from workflows.strategy_attribution_policy import AttributionPolicySnapshot


def _base_request(**overrides) -> BacktestWorkflowRequest:
    values = dict(
        start_dt=date(2026, 1, 1),
        end_dt=date(2026, 1, 31),
        hold_days=5,
        top_n=0,
        board="all",
        sample_size=0,
        trading_days=320,
        max_workers=1,
        exit_mode="atr",
    )
    values.update(overrides)
    return BacktestWorkflowRequest(**values)


def test_run_backtest_workflow_builds_context_without_network(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("FUNNEL_SMALLCAP_BENCH_CODE", "399905")

    monkeypatch.setattr(
        "workflows.backtest.resolve_backtest_universe",
        lambda *_args, **_kwargs: BacktestUniverse(["000001"], {"000001": "平安银行"}, "test"),
    )
    monkeypatch.setattr(
        "workflows.backtest.load_backtest_history",
        lambda **_kwargs: BacktestHistory({"000001": pd.DataFrame()}, pd.DataFrame(), [], 3, True),
    )
    monkeypatch.setattr(
        "workflows.backtest.load_backtest_metadata",
        lambda *_args, **_kwargs: BacktestMetadata(
            {"000001": 100.0},
            {"000001": "银行"},
            {"000001": ["CPO"]},
            [{"name": "CPO", "pct": 3.2}],
            {"000001": {"roe": 12}},
            "test",
        ),
    )

    def fake_execute_backtest_run(**kwargs):
        captured.update(kwargs)
        return pd.DataFrame(), {"ok": True}

    monkeypatch.setattr("workflows.backtest.execute_backtest_run", fake_execute_backtest_run)

    trades, summary = run_backtest_request(
        BacktestWorkflowRequest(
            start_dt=date(2026, 1, 1),
            end_dt=date(2026, 1, 31),
            hold_days=10,
            top_n=4,
            board="all",
            sample_size=0,
            trading_days=320,
            max_workers=1,
            cash_portfolio=True,
            portfolio_styles="confirmation_only",
        )
    )

    assert trades.empty
    assert summary == {"ok": True}
    assert captured["context"].board == "all"
    assert captured["data"].name_map == {"000001": "平安银行"}
    assert captured["data"].concept_map == {"000001": ["CPO"]}
    assert captured["data"].concept_heat == [{"name": "CPO", "pct": 3.2}]
    assert captured["data"].financial_map == {"000001": {"roe": 12}}
    assert captured["config"].performance.cash_portfolio is True
    analyzer = captured["config"].replay.market_regime_analyzer
    assert analyzer.keywords["regime_config"].smallcap_bench_code == "399905"


def test_backtest_signal_weight_map_matches_funnel_policy_gate(monkeypatch) -> None:
    import workflows.backtest as backtest

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "shadow")
    monkeypatch.setattr(
        backtest,
        "load_attribution_policy_snapshot",
        lambda **_kwargs: AttributionPolicySnapshot(
            weights={"lps": 0.5},
            source="远端",
            report_date="2026-07-04",
            execution_policy="shadow",
            execution_scope="funnel_shadow",
            formal_dynamic_allowed=False,
            formal_dynamic_block_reason="auto_apply=false",
        ),
    )

    shadow_weights, shadow_meta = backtest._signal_policy_from_env()
    assert shadow_weights == {"lps": 0.5}
    assert shadow_meta["source"] == "远端"
    assert shadow_meta["active_scope"] == "漏斗shadow"
    assert shadow_meta["formal_dynamic_allowed"] is False
    assert shadow_meta["formal_dynamic_block_reason"] == "auto_apply=false"
    assert backtest._signal_weight_map_from_env() == {"lps": 0.5}

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "on")
    monkeypatch.setattr(
        backtest,
        "load_attribution_policy_snapshot",
        lambda **_kwargs: AttributionPolicySnapshot(
            weights={"lps": 0.5},
            formal_dynamic_allowed=False,
            formal_dynamic_block_reason="next_action=keep_static_policy",
        ),
    )

    blocked_weights, blocked_meta = backtest._signal_policy_from_env()
    assert blocked_weights == {}
    assert blocked_meta["formal_dynamic_allowed"] is False
    assert blocked_meta["formal_dynamic_block_reason"] == "next_action=keep_static_policy"
    assert backtest._signal_weight_map_from_env() == {}

    monkeypatch.setattr(
        backtest,
        "load_attribution_policy_snapshot",
        lambda **_kwargs: AttributionPolicySnapshot(
            weights={"sos": 1.15},
            source="远端",
            report_date="2026-07-04",
            horizon="5",
            execution_policy="on",
            execution_scope="funnel_formal",
            formal_dynamic_allowed=True,
        ),
    )

    weights, meta = backtest._signal_policy_from_env()
    assert weights == {"sos": 1.15}
    assert meta["source"] == "远端"
    assert meta["report_date"] == "2026-07-04"
    assert meta["active_scope"] == "正式漏斗"
    assert backtest._signal_weight_map_from_env() == {"sos": 1.15}


def test_shared_request_key_ignores_atr_params_for_signal_reuse() -> None:
    import workflows.backtest as backtest

    baseline = _base_request(atr_multiplier=2.0, atr_hard_stop_pct=-12.0, atr_period=14)
    grid_variant = _base_request(atr_multiplier=4.0, atr_hard_stop_pct=-18.0, atr_period=20)

    assert backtest._shared_request_key(baseline) == backtest._shared_request_key(grid_variant)
    backtest._validate_shared_signal_suite([baseline, grid_variant])


def test_validate_shared_signal_suite_rejects_non_exit_param_changes() -> None:
    import workflows.backtest as backtest

    baseline = _base_request(board="all")
    mismatched = _base_request(board="main")

    with pytest.raises(ValueError, match="复用信号台账"):
        backtest._validate_shared_signal_suite([baseline, mismatched])


def test_validate_shared_signal_suite_allows_weight_only_strategy_variants() -> None:
    import workflows.backtest as backtest

    requests = [_base_request(strategy_variant=variant) for variant in ("A", "M", "P")]

    backtest._validate_shared_signal_suite(requests)


def test_validate_shared_signal_suite_allows_no_backfill_replay_gates() -> None:
    import workflows.backtest as backtest

    backtest._validate_shared_signal_suite([_base_request(strategy_variant="A"), _base_request(strategy_variant="F")])


def test_validate_shared_signal_suite_rejects_signal_build_changes() -> None:
    import workflows.backtest as backtest

    with pytest.raises(ValueError, match="只能改变重放阶段"):
        backtest._validate_shared_signal_suite(
            [_base_request(strategy_variant="A"), _base_request(strategy_variant="H")]
        )
