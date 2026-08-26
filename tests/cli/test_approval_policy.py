from __future__ import annotations

import pytest

from cli.approval_policy import (
    AUTO,
    AUTO_TOOLS,
    CONFIRM,
    REVIEW,
    classify,
    explain,
    nav_ratio,
    notional,
)


class TestAutoTier:
    def test_set_stop_loss_is_auto(self):
        assert classify("set_stop_loss", {"code": "002270", "stop_loss": 33.15}) == AUTO

    def test_set_stop_loss_batch_is_auto(self):
        args = {"items": [{"code": "002270", "stop_loss": 33.15}] * 50}
        assert classify("set_stop_loss", args) == AUTO

    def test_auto_needs_both_tool_identity_and_a_safe_arg_shape(self):
        """光看工具名不够了 —— 还要看这组参数是哪个方向。

        原来这条测试叫 test_auto_rests_on_tool_identity_not_field_check，断言
        「不接受 shares/cost_price 所以无需检查参数」。在 set_stop_loss 支持
        stop_loss=None 清除止损之后，那个前提就不成立了：同一个工具既能加一道
        保护也能撤掉一道，风险方向相反。
        """
        import inspect

        from agents.portfolio_tools import set_stop_loss

        # 窄接口仍然是前提之一：它确实不能动股数/成本/现金
        params = set(inspect.signature(set_stop_loss).parameters)
        assert params == {"code", "stop_loss", "items", "tool_context"}

        # 但「在 AUTO_TOOLS 里」不再等于放行
        assert "set_stop_loss" in AUTO_TOOLS
        assert classify("set_stop_loss", {"code": "002270", "stop_loss": None}) == REVIEW

    def test_batch_cannot_smuggle_a_clear(self):
        """批量把动作藏在数组里，只查顶层字段会漏 —— 撤掉多只票的止损更该拦。"""
        args = {"items": [{"code": "A", "stop_loss": 33.15}, {"code": "B", "stop_loss": None}]}
        assert classify("set_stop_loss", args) == REVIEW

    def test_update_portfolio_is_never_auto(self):
        assert classify("update_portfolio", {"code": "002270", "shares": 500}) != AUTO


class TestConfirmTier:
    def test_sell_requires_confirm(self):
        assert classify("update_portfolio", {"action": "sell", "code": "605007"}) == CONFIRM

    def test_remove_requires_confirm(self):
        assert classify("update_portfolio", {"action": "remove", "code": "605007"}) == CONFIRM

    def test_delete_records_requires_confirm(self):
        """MCP 支持的批量删记录，此前会落到 review。"""
        args = {"action": "delete_records", "table": "signal", "codes": ["1", "2"]}
        assert classify("update_portfolio", args) == CONFIRM

    def test_trade_fill_sell_requires_confirm(self):
        args = {"code": "605007", "side": "sell", "shares": 100, "price": 12.0}
        assert classify("record_trade_fill", args) == CONFIRM

    def test_over_five_percent_of_nav(self):
        args = {"action": "add", "code": "600519", "shares": 100, "cost_price": 1452.0}
        assert classify("update_portfolio", args, nav=1_000_000.0) == CONFIRM

    def test_just_under_threshold_is_review(self):
        args = {"action": "add", "code": "600519", "shares": 10, "cost_price": 1452.0}
        assert classify("update_portfolio", args, nav=1_000_000.0) == REVIEW


class TestBatchItems:
    def test_destructive_item_escalates(self):
        """批量里藏一条 sell，不能因为顶层 action 是 update 就放过。"""
        args = {
            "action": "update",
            "items": [
                {"code": "002270", "shares": 100, "cost_price": 30.0},
                {"code": "605007", "action": "sell"},
            ],
        }
        assert classify("update_portfolio", args, nav=1_000_000.0) == CONFIRM

    def test_single_large_item_escalates(self):
        args = {"action": "update", "items": [{"code": "600519", "shares": 100, "cost_price": 1452.0}]}
        assert classify("update_portfolio", args, nav=1_000_000.0) == CONFIRM

    def test_aggregate_over_threshold_escalates(self):
        """单条都不超阈值，但合计超——批量的真实风险在总额。"""
        args = {
            "action": "update",
            "items": [{"code": f"00{i}", "shares": 100, "cost_price": 200.0} for i in range(4)],
        }
        assert classify("update_portfolio", args, nav=1_000_000.0) == CONFIRM

    def test_small_batch_stays_review(self):
        args = {"action": "update", "items": [{"code": "002270", "shares": 10, "cost_price": 30.0}]}
        assert classify("update_portfolio", args, nav=1_000_000.0) == REVIEW

    def test_malformed_item_escalates(self):
        args = {"action": "update", "items": ["not-a-dict"]}
        assert classify("update_portfolio", args) == CONFIRM


class TestReviewTier:
    def test_other_write_tools_always_review(self):
        for tool in ("exec_command", "write_file"):
            assert classify(tool, {"code": "002270", "stop_loss": 33.15}) == REVIEW

    def test_small_buy_fill_is_review(self):
        args = {"code": "002270", "side": "buy", "shares": 100, "price": 10.0}
        assert classify("record_trade_fill", args, nav=100_000.0) == REVIEW

    def test_missing_nav_falls_back_to_review(self):
        args = {"action": "add", "code": "600519", "shares": 100, "cost_price": 1452.0}
        assert classify("update_portfolio", args, nav=0.0) == REVIEW


class TestNotional:
    def test_uses_cost_price(self):
        assert notional({"shares": 100, "cost_price": 12.5}) == 1250.0

    def test_zero_when_incomplete(self):
        assert notional({"shares": 100}) == 0.0

    def test_handles_bad_types(self):
        assert notional({"shares": "abc", "cost_price": 10}) == 0.0


class TestExplain:
    """理由只用于展示，但必须和 classify 的判定路径一致，否则界面会解释错。"""

    def test_destructive_beats_amount(self):
        args = {"action": "sell", "code": "605007", "shares": 1, "cost_price": 1.0}
        assert explain("update_portfolio", args, nav=1_000_000.0) == "reason.destructive_action"

    def test_sell_fill_is_destructive(self):
        args = {"code": "605007", "side": "sell", "shares": 100, "price": 12.0}
        assert explain("record_trade_fill", args) == "reason.destructive_action"

    def test_over_nav(self):
        args = {"action": "add", "code": "600519", "shares": 100, "cost_price": 1452.0}
        assert explain("update_portfolio", args, nav=1_000_000.0) == "reason.over_nav"

    def test_batch_aggregate_names_the_aggregate(self):
        """单条都不超但合计超时，理由必须说是合计，否则用户会去逐条找那个大的。"""
        args = {"action": "update", "items": [{"code": f"00{i}", "shares": 100, "cost_price": 200.0} for i in range(4)]}
        assert explain("update_portfolio", args, nav=1_000_000.0) == "reason.batch_over_nav"

    def test_batch_malformed(self):
        args = {"action": "update", "items": ["not-a-dict"]}
        assert explain("update_portfolio", args) == "reason.batch_malformed"

    def test_missing_nav_is_not_reported_as_small(self):
        """净值拿不到时不能沉默——否则用户以为系统判定金额不大。"""
        args = {"action": "add", "code": "600519", "shares": 100, "cost_price": 1452.0}
        assert explain("update_portfolio", args, nav=0.0) == "reason.nav_unknown"

    def test_plain_write_tool(self):
        assert explain("exec_command", {"cmd": "ls"}) == "reason.write_tool"

    def test_auto_tool(self):
        assert explain("set_stop_loss", {"code": "002270", "stop_loss": 33.15}) == "reason.auto_narrow_tool"

    def test_confirm_tier_never_gets_the_generic_reason(self):
        """confirm 档必须有具体理由：显示「按规则要人过一遍」等于没解释。"""
        confirm_cases = [
            ("update_portfolio", {"action": "sell", "code": "1"}),
            ("update_portfolio", {"action": "add", "shares": 100, "cost_price": 1452.0}),
            ("update_portfolio", {"action": "update", "items": ["bad"]}),
        ]
        for tool, args in confirm_cases:
            assert classify(tool, args, nav=1_000_000.0) == CONFIRM
            assert explain(tool, args, nav=1_000_000.0) != "reason.write_tool"


class TestNavRatio:
    def test_single_item(self):
        args = {"shares": 100, "cost_price": 500.0}
        assert nav_ratio(args, 1_000_000.0) == pytest.approx(0.05)

    def test_batch_sums(self):
        args = {"items": [{"shares": 100, "cost_price": 200.0}] * 3}
        assert nav_ratio(args, 1_000_000.0) == pytest.approx(0.06)

    def test_zero_without_nav(self):
        assert nav_ratio({"shares": 100, "cost_price": 10.0}, 0.0) == 0.0

    def test_ignores_malformed_batch_entries(self):
        args = {"items": [{"shares": 100, "cost_price": 200.0}, "bad"]}
        assert nav_ratio(args, 1_000_000.0) == pytest.approx(0.02)
