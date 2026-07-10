"""Tests for core.tail_buy.decision_semantics."""

from __future__ import annotations

from core.tail_buy.decision_semantics import is_limit_up_candidate, tail_buy_execution_semantics


class TestIsLimitUpCandidate:
    def test_touched_flag_true(self):
        assert is_limit_up_candidate({"limit_up_touched": True}) is True

    def test_closed_flag_true(self):
        assert is_limit_up_candidate({"limit_up_closed": True}) is True

    def test_missing_features_false(self):
        assert is_limit_up_candidate(None) is False
        assert is_limit_up_candidate({}) is False

    def test_false_flags_false(self):
        assert is_limit_up_candidate({"limit_up_touched": False, "limit_up_closed": False}) is False


class TestTailBuyExecutionSemanticsLimitUp:
    def test_limit_up_buy_downgrades_to_watch_only(self):
        semantics = tail_buy_execution_semantics("BUY", "spring", features={"limit_up_touched": True})

        assert semantics["execution_label"] == "观察买入"
        assert semantics["execution_status"] == "watch_buy"
        assert semantics["orderable"] is False
        assert "涨停" in semantics["execution_next_step"]

    def test_normal_buy_without_limit_up_stays_executable(self):
        semantics = tail_buy_execution_semantics("BUY", "spring", features={"limit_up_touched": False})

        assert semantics["execution_label"] == "可执行买入"
        assert semantics["orderable"] is True

    def test_high_risk_momentum_signal_still_downgrades_without_limit_up(self):
        semantics = tail_buy_execution_semantics("BUY", "rec_momentum_continuation", features={})

        assert semantics["execution_label"] == "观察买入"
        assert semantics["orderable"] is False
        assert "高位动能" in semantics["execution_next_step"]

    def test_explicit_persisted_fields_take_precedence_over_fallback(self):
        persisted = {
            "limit_up_touched": True,
            "execution_label": "可执行买入",
            "execution_status": "executable_buy",
            "orderable": True,
            "execution_next_step": "人工已复核确认可挂单。",
        }

        semantics = tail_buy_execution_semantics("BUY", "spring", features=persisted)

        assert semantics["execution_label"] == "可执行买入"
        assert semantics["orderable"] is True

    def test_post_close_review_mode_ignores_limit_up_fallback(self):
        semantics = tail_buy_execution_semantics(
            "BUY", "spring", report_mode="post_close_review", features={"limit_up_touched": True}
        )

        assert semantics["execution_label"] == "明日观察买入"
        assert semantics["orderable"] is False
