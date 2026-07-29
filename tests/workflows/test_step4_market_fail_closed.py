from core.market_trade_mode import EXECUTE_BLOCK_NEW_BUY_REGIMES, KNOWN_MARKET_REGIMES, MARKET_EXECUTION_PRIORITY
from workflows.step4_market import (
    build_market_guardrail,
    data_gap_blocks_buying,
    missing_market_inputs,
    normalize_premarket_regime,
    resolve_effective_market_regime,
)

_READY = {"status": "ok", "reason": ""}


def test_invalid_premarket_regime_fails_closed() -> None:
    assert normalize_premarket_regime("typo") == "UNKNOWN"
    assert resolve_effective_market_regime("NEUTRAL", "typo") == "UNKNOWN"


def test_missing_premarket_regime_fails_closed() -> None:
    assert normalize_premarket_regime(None) == "UNKNOWN"
    assert resolve_effective_market_regime("NEUTRAL", None) == "UNKNOWN"


def test_repair_stages_survive_normal_premarket_merge() -> None:
    assert resolve_effective_market_regime("PANIC_REPAIR", "NORMAL") == "PANIC_REPAIR"
    assert resolve_effective_market_regime("PANIC_REPAIR_CONFIRMED", "NORMAL") == "PANIC_REPAIR_CONFIRMED"
    assert resolve_effective_market_regime("PANIC_REPAIR_CONFIRMED", "CAUTION") == "PANIC_REPAIR_CONFIRMED"
    assert resolve_effective_market_regime("PANIC_REPAIR_CONFIRMED", "RISK_OFF") == "RISK_OFF"


def test_caution_and_risk_on_keep_their_execution_semantics() -> None:
    assert resolve_effective_market_regime("CAUTION", "NORMAL") == "CAUTION"
    assert resolve_effective_market_regime("RISK_ON", "NORMAL") == "RISK_ON"
    assert resolve_effective_market_regime("RISK_ON", "CAUTION") == "RISK_ON"


def test_every_emitted_benchmark_regime_has_explicit_execution_priority() -> None:
    assert KNOWN_MARKET_REGIMES <= MARKET_EXECUTION_PRIORITY.keys()


def test_no_source_level_hard_block_can_escape_after_merge() -> None:
    benchmark_regimes = KNOWN_MARKET_REGIMES | {"UNKNOWN"}
    premarket_regimes = {"UNKNOWN", "NORMAL", "CAUTION", "RISK_OFF", "BLACK_SWAN"}
    for benchmark in benchmark_regimes:
        for premarket in premarket_regimes:
            effective = resolve_effective_market_regime(benchmark, premarket)
            if benchmark in EXECUTE_BLOCK_NEW_BUY_REGIMES or premarket in EXECUTE_BLOCK_NEW_BUY_REGIMES:
                assert effective in EXECUTE_BLOCK_NEW_BUY_REGIMES, f"{benchmark}+{premarket} escaped as {effective}"


def test_absent_premarket_is_distinguished_from_a_real_unknown_verdict() -> None:
    """两者都会降级为 UNKNOWN 禁买，但只有前者是运维可以补救的。"""
    assert missing_market_inputs("NEUTRAL", None, _READY, None) == ["premarket"]
    assert missing_market_inputs("NEUTRAL", "UNKNOWN", _READY, None) == []


def test_benchmark_written_as_blank_counts_as_a_gap_even_when_the_row_exists() -> None:
    """market_signal_daily 有当日行、但 benchmark_regime 为空，readiness 只报 partial。"""
    partial = {"status": "partial", "reason": "当日盘后 benchmark 尚未就绪"}
    assert missing_market_inputs(None, "NORMAL", partial, None) == ["benchmark"]


def test_stale_benchmark_is_reported_only_when_no_live_context_overrides_it() -> None:
    stale = {"status": "stale", "reason": "trade_date 落后"}
    assert missing_market_inputs("NEUTRAL", "NORMAL", stale, None) == ["benchmark"]
    assert missing_market_inputs("NEUTRAL", "NORMAL", stale, {"regime": "NEUTRAL"}) == []


def test_data_gap_is_only_decisive_when_filling_it_would_actually_unblock_buying() -> None:
    blocks = {"UNKNOWN", "CRASH", "RISK_OFF"}
    assert data_gap_blocks_buying("NEUTRAL", ["premarket"], blocks) is True
    # 收盘态本身就是 CRASH，补齐盘前也照样禁买，不该甩锅给数据。
    assert data_gap_blocks_buying("CRASH", ["premarket"], blocks) is False
    assert data_gap_blocks_buying("NEUTRAL", [], blocks) is False


def test_crash_day_missing_premarket_does_not_blame_the_data_pipeline() -> None:
    _regime, guardrail_text, market_view = build_market_guardrail(
        trade_date="2026-07-28",
        benchmark_context={"regime": "CRASH"},
        market_signal_row={"trade_date": "2026-07-28", "benchmark_regime": "CRASH"},
        buy_block_regimes={"CRASH", "UNKNOWN"},
    )

    assert "一票否决" in guardrail_text
    assert "数据缺失" not in guardrail_text
    assert "禁买源自数据缺失" not in market_view


def test_buy_block_caused_by_missing_data_says_so_in_guardrail_and_market_view() -> None:
    _regime, guardrail_text, market_view = build_market_guardrail(
        trade_date="2026-07-21",
        benchmark_context={"regime": "NEUTRAL"},
        market_signal_row={"trade_date": "2026-07-21", "benchmark_regime": "NEUTRAL"},
        buy_block_regimes={"UNKNOWN"},
    )

    assert "数据缺失" in guardrail_text
    assert "premarket_risk" in guardrail_text
    assert "禁买源自数据缺失" in market_view


def test_buy_block_caused_by_real_market_stress_is_not_blamed_on_data() -> None:
    _regime, guardrail_text, market_view = build_market_guardrail(
        trade_date="2026-07-21",
        benchmark_context={"regime": "CRASH"},
        market_signal_row={
            "trade_date": "2026-07-21",
            "benchmark_regime": "CRASH",
            "premarket_regime": "RISK_OFF",
        },
        buy_block_regimes={"CRASH"},
    )

    assert "一票否决" in guardrail_text
    assert "数据缺失" not in guardrail_text
    assert "禁买源自数据缺失" not in market_view


def test_worsening_premarket_never_increases_execution_permission() -> None:
    def permission(regime: str) -> int:
        if regime in EXECUTE_BLOCK_NEW_BUY_REGIMES:
            return 0
        if regime in {"CAUTION", "PANIC_REPAIR_CONFIRMED", "PANIC_REPAIR_INTRADAY"}:
            return 1
        return 2

    for benchmark in KNOWN_MARKET_REGIMES | {"UNKNOWN"}:
        permissions = [
            permission(resolve_effective_market_regime(benchmark, premarket))
            for premarket in ("NORMAL", "CAUTION", "RISK_OFF", "BLACK_SWAN")
        ]
        assert permissions == sorted(permissions, reverse=True), f"{benchmark}: {permissions}"
