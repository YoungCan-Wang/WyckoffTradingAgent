"""Tests for agents.portfolio_tools extreme-day intraday fetch gating."""

from __future__ import annotations

import pandas as pd
import pytest

from agents import portfolio_tools


def _df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


class TestFetchIntradayIfExtremeDay:
    def test_mild_day_change_skips_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TICKFLOW_API_KEY", "dummy-key")
        df = _df([10.0, 9.8])  # -2%, below the -5% threshold
        result = portfolio_tools._fetch_intraday_if_extreme_day("600519", df)
        assert result is None

    def test_insufficient_history_skips_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TICKFLOW_API_KEY", "dummy-key")
        df = _df([10.0])
        assert portfolio_tools._fetch_intraday_if_extreme_day("600519", df) is None

    def test_missing_api_key_skips_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
        df = _df([10.0, 9.0])  # -10%, exceeds threshold
        result = portfolio_tools._fetch_intraday_if_extreme_day("600519", df)
        assert result is None

    def test_extreme_drop_with_api_key_fetches_intraday(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TICKFLOW_API_KEY", "dummy-key")
        sentinel = pd.DataFrame({"close": [9.0]})

        class _FakeClient:
            def __init__(self, api_key: str) -> None:
                assert api_key == "dummy-key"

            def get_intraday(self, code: str, *, period: str, count: int) -> pd.DataFrame:
                assert code == "600519"
                return sentinel

        monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", _FakeClient)
        df = _df([10.0, 9.0])  # -10%, exceeds threshold
        result = portfolio_tools._fetch_intraday_if_extreme_day("600519", df)
        assert result is sentinel

    def test_client_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TICKFLOW_API_KEY", "dummy-key")

        class _RaisingClient:
            def __init__(self, api_key: str) -> None:
                pass

            def get_intraday(self, *args, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", _RaisingClient)
        df = _df([10.0, 9.0])
        result = portfolio_tools._fetch_intraday_if_extreme_day("600519", df)
        assert result is None


class TestPortfolioViewStopLoss:
    """view 必须带出 stop_loss。

    模型只能看到工具返回的字段：漏掉它，「哪些持仓没设止损」就会全部答错，
    而且答得很自信 —— 这是风控数字，不能靠字段缺失去推断。
    """

    def test_view_preserves_stop_loss(self) -> None:
        state = {
            "free_cash": 1000.0,
            "positions": [
                {"code": "600519", "name": "贵州茅台", "shares": 200, "cost": 1452.0, "stop_loss": 1380.5},
                {"code": "002270", "name": "法狮龙", "shares": 2500, "cost": 30.84, "stop_loss": None},
            ],
        }
        view = portfolio_tools._portfolio_view("pf-1", state)
        stops = {p["code"]: p["stop_loss"] for p in view["positions"]}
        assert stops == {"600519": 1380.5, "002270": None}

    def test_view_missing_stop_loss_count_is_accurate(self) -> None:
        state = {
            "free_cash": 0.0,
            "positions": [
                {"code": "600519", "name": "贵州茅台", "shares": 200, "cost": 1452.0, "stop_loss": 1380.5},
                {"code": "002270", "name": "法狮龙", "shares": 2500, "cost": 30.84},
            ],
        }
        view = portfolio_tools._portfolio_view("pf-1", state)
        missing = [p for p in view["positions"] if p.get("stop_loss") is None]
        assert len(missing) == 1
        assert missing[0]["code"] == "002270"

    def test_missing_stop_loss_stays_none_not_zero(self) -> None:
        """真的没设过要是 None；给成 0 会被读成「止损价 0 元」。"""
        state = {"positions": [{"code": "002270", "shares": 100, "cost": 30.0}], "free_cash": 0}
        view = portfolio_tools._portfolio_view("pid", state)
        assert view["positions"][0]["stop_loss"] is None

    def test_stop_loss_survives_the_tool_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """端到端走 portfolio(mode="view")：TUI 与 MCP 都经过这条路径。"""
        state = {
            "positions": [{"code": "002270", "shares": 100, "cost": 30.0, "stop_loss": 27.5}],
            "free_cash": 0,
        }
        monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx: "pid")
        monkeypatch.setattr(portfolio_tools, "_load_portfolio_state", lambda _pid, _ctx: state)

        result = portfolio_tools.portfolio(mode="view")

        assert result["positions"][0]["stop_loss"] == 27.5


class TestCloudFailureFallsBackToLocal:
    """云端读失败必须落到本地库，而不是把异常抛给界面。

    实测踩到的：切到持仓页显示「暂无持仓数据」，而本地库里 8 只持仓一直都在。
    根因是 `_load_portfolio_state` 里云端那两行**没有包 try**：

        client = get_user_client(tool_context)   # 网络异常直接穿透
        state = with_auth_retry(...)
        ...
        try:                                     # 写好的本地兜底根本到不了
            return load_portfolio(portfolio_id)

    「云端挂了」和「你没有持仓」是两件完全不同的事。
    """

    def test_cloud_exception_falls_back_to_local(self, monkeypatch) -> None:
        import agents.portfolio_tools as pt

        local_state = {"positions": [{"code": "000001", "shares": 100}], "free_cash": 50.0}
        monkeypatch.setattr(pt, "has_cloud", lambda ctx: True)
        monkeypatch.setattr(
            pt,
            "get_user_client",
            lambda ctx: (_ for _ in ()).throw(Exception("handshake timed out")),
        )
        monkeypatch.setitem(
            __import__("sys").modules,
            "integrations.local_db",
            type("M", (), {"load_portfolio": staticmethod(lambda pid: dict(local_state))}),
        )

        got = pt._load_portfolio_state("USER_LIVE:x", object())

        assert got is not None, "云端失败时返回了 None —— 本地兜底没生效"
        assert len(got["positions"]) == 1
        # 必须标注来源：本地数据可能落后于另一台设备的改动，界面要能说出来
        assert got.get("source") == "local"
        assert "handshake timed out" in str(got.get("cloud_error"))

    def test_no_source_tag_when_cloud_was_never_tried(self, monkeypatch) -> None:
        """未登录时读本地是正常路径，不该标成「降级」。"""
        import agents.portfolio_tools as pt

        monkeypatch.setattr(pt, "has_cloud", lambda ctx: False)
        monkeypatch.setitem(
            __import__("sys").modules,
            "integrations.local_db",
            type("M", (), {"load_portfolio": staticmethod(lambda pid: {"positions": [], "free_cash": 0})}),
        )

        got = pt._load_portfolio_state("USER_LIVE:x", None)

        assert got is not None
        assert "source" not in got, "没试过云端就不该标降级"
        assert "cloud_error" not in got


class TestValuationSurvivesOffline:
    """总估值要能在云端断线时仍然显示出来。

    背景：估值原来只在云端算、也只在云端存，本地 portfolio 表没有这一列。加了
    「云端连不上就用本地库」的兜底之后暴露出来：持仓看得到，总资产必然是
    「—/未估值」。用户的第一反应是「持仓成本加现金不就是总资产吗，这怎么还能
    算不出来」—— 而那个公式是错的（成本是买入价，估值要用市价），但「算不出来」
    确实是个真问题。
    """

    def test_cloud_read_caches_equity_locally(self, monkeypatch, tmp_path) -> None:
        import agents.portfolio_tools as pt

        saved = {}

        def fake_save(pid, cash, positions, total_equity=None):
            saved.update(pid=pid, cash=cash, equity=total_equity)

        monkeypatch.setitem(
            __import__("sys").modules,
            "integrations.local_db",
            type("M", (), {"save_portfolio": staticmethod(fake_save)}),
        )
        pt._cache_portfolio("pid", {"free_cash": 100.0, "positions": [], "total_equity": 92858.41}, "remote")

        assert saved.get("equity") == 92858.41, "云端算好的估值没被缓存下来"

    def test_missing_equity_passes_none_not_zero(self, monkeypatch) -> None:
        """拿不到估值时要传 None —— 传 0 会把「没估过」写成「估值为 0」。"""
        import agents.portfolio_tools as pt

        seen = {}
        monkeypatch.setitem(
            __import__("sys").modules,
            "integrations.local_db",
            type(
                "M",
                (),
                {
                    "save_portfolio": staticmethod(
                        lambda pid, cash, pos, total_equity=None: seen.update(equity=total_equity)
                    )
                },
            ),
        )
        pt._cache_portfolio("pid", {"free_cash": 0, "positions": []}, "remote")

        assert seen["equity"] is None

    def test_view_reads_valued_at_for_local_data(self) -> None:
        """本地那份的时间戳在 valued_at，云端那份在 updated_at —— 两者都要认。

        不认 valued_at 的话，兜底给出的估值会显示成「没有时间」。一个不标时间的
        旧估值比不显示更容易误导：用户会以为那是刚算的。
        """
        import agents.portfolio_tools as pt

        state = {
            "free_cash": 0,
            "positions": [],
            "total_equity": 1234.5,
            "valued_at": "2026-08-25 11:25:33",
        }
        view = pt._portfolio_view("pid", state)
        assert view["total_equity"] == 1234.5
        assert view["valuation_updated_at"] == "2026-08-25 11:25:33"

    def test_zero_equity_is_still_a_valuation(self) -> None:
        """清仓且无现金时估值是 0，不能被当成「没估过」而省掉字段。"""
        import agents.portfolio_tools as pt

        view = pt._portfolio_view("pid", {"free_cash": 0, "positions": [], "total_equity": 0})
        assert "total_equity" in view
        assert view["total_equity"] == 0
