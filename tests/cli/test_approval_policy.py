from __future__ import annotations

from cli.approval_policy import AUTO, CONFIRM, REVIEW, classify, is_auto_tool, notional


class TestAutoTier:
    def test_set_stop_loss_is_auto(self):
        assert classify("set_stop_loss", {"code": "002270", "stop_loss": 33.15}) == AUTO

    def test_set_stop_loss_batch_is_auto(self):
        args = {"items": [{"code": "002270", "stop_loss": 33.15}] * 50}
        assert classify("set_stop_loss", args) == AUTO

    def test_auto_rests_on_tool_identity_not_field_check(self):
        """set_stop_loss 不接受 shares/cost_price，所以无需靠参数检查保证安全。"""
        import inspect

        from agents.portfolio_tools import set_stop_loss

        params = set(inspect.signature(set_stop_loss).parameters)
        assert params == {"code", "stop_loss", "items", "tool_context"}
        assert is_auto_tool("set_stop_loss")

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
