from __future__ import annotations

import pytest

from tools.write_guard import ALLOW_ENV, WRITE_TOOLS, check_write_allowed, is_write_tool, writes_allowed


@pytest.fixture(autouse=True)
def _clear_allow_env(monkeypatch):
    monkeypatch.delenv(ALLOW_ENV, raising=False)


class TestWriteToolSet:
    def test_portfolio_writes_are_guarded(self):
        assert is_write_tool("update_portfolio")
        assert is_write_tool("record_trade_fill")
        assert is_write_tool("set_stop_loss")

    def test_local_side_effects_are_guarded(self):
        assert is_write_tool("exec_command")
        assert is_write_tool("write_file")

    def test_read_tools_are_not_guarded(self):
        for name in ("portfolio", "analyze_stock", "screen_stocks", "query_history"):
            assert not is_write_tool(name)

    def test_matches_cli_approval_set(self):
        """两处必须一致：tools/ 不能 import cli/，所以列表是手抄的。"""
        from cli.tools import CONFIRM_TOOLS

        assert WRITE_TOOLS == frozenset(CONFIRM_TOOLS)


class TestDefaultDeny:
    def test_writes_denied_by_default(self):
        assert writes_allowed() is False
        denied = check_write_allowed("update_portfolio")
        assert denied is not None
        assert denied["status"] == "error"

    def test_denial_explains_where_to_go(self):
        denied = check_write_allowed("update_portfolio")
        assert "审批" in denied["error"]
        assert ALLOW_ENV in denied["error"]

    def test_reads_pass_through(self):
        assert check_write_allowed("portfolio") is None


class TestExplicitOptIn:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
    def test_opt_in_values(self, monkeypatch, value):
        monkeypatch.setenv(ALLOW_ENV, value)
        assert writes_allowed() is True
        assert check_write_allowed("update_portfolio") is None

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
    def test_non_opt_in_values_still_deny(self, monkeypatch, value):
        monkeypatch.setenv(ALLOW_ENV, value)
        assert writes_allowed() is False
        assert check_write_allowed("update_portfolio") is not None


class TestMcpEntrypoints:
    """MCP 工具必须真的走到闸门——此前 update_portfolio 直接调底层函数。"""

    def test_update_portfolio_refuses_without_touching_data(self, monkeypatch):
        import mcp_server

        called = False

        def _boom(**_kwargs):
            nonlocal called
            called = True
            raise AssertionError("write reached the data layer")

        monkeypatch.setattr(mcp_server, "_update_portfolio", _boom)
        result = mcp_server.update_portfolio(action="remove", code="605007")
        assert result["status"] == "error"
        assert called is False

    def test_record_trade_fill_refuses_without_touching_data(self, monkeypatch):
        import mcp_server

        def _boom(**_kwargs):
            raise AssertionError("write reached the data layer")

        monkeypatch.setattr(mcp_server, "_record_trade_fill", _boom)
        result = mcp_server.record_trade_fill(code="605007", side="sell", shares=100, price=13.0)
        assert result["status"] == "error"
