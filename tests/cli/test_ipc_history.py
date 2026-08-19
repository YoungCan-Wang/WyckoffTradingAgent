"""桌面 IPC 的跟踪与归因方法。

同持仓那次的教训：不把会话上下文传下去，has_cloud() 恒为 False，用户读到的是
本地缓存而不是自己的云端数据，而且完全静默。这里锁住上下文透传、limit 钳制，
以及失败要报错而不是伪装成空结果。
"""

from __future__ import annotations

from typing import Any

import pytest

from cli.ipc import methods
from cli.ipc.methods import MethodError


def _events(name: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return list(methods.dispatch(name, params or {}))


def _result(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    for event in _events(name, params):
        if event.get("type") == "result":
            return event
    return {}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """替换 query_history，记录它收到的参数。"""
    seen: dict[str, Any] = {}
    sentinel = object()

    class FakeSession:
        tool_context = sentinel

    def fake_query(
        source: str,
        limit: int = 20,
        tool_context: Any = None,
        market: str = "cn",
        **_kw: Any,
    ) -> dict[str, Any]:
        seen["source"] = source
        seen["limit"] = limit
        seen["tool_context"] = tool_context
        seen["market"] = market
        return {"total": 0, "records": []}

    monkeypatch.setattr("agents.history_tools.query_history", fake_query)
    monkeypatch.setattr("cli.ipc.session.get_session", lambda: FakeSession())
    seen["sentinel"] = sentinel
    return seen


class TestAuthContext:
    def test_tracking_passes_session_context(self, captured: dict[str, Any]) -> None:
        _result("tracking")
        assert captured["source"] == "recommendation"
        assert captured["tool_context"] is captured["sentinel"]

    def test_attribution_passes_session_context(self, captured: dict[str, Any]) -> None:
        _result("attribution")
        assert captured["source"] == "attribution"
        assert captured["tool_context"] is captured["sentinel"]


class TestLimitClamping:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [(None, 200), (1, 1), (200, 200), (999, 200), (0, 1), (-5, 1), ("abc", 200)],
    )
    def test_tracking_limit(self, captured: dict[str, Any], given: Any, expected: int) -> None:
        _result("tracking", {"limit": given})
        assert captured["limit"] == expected

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(None, 20), (1, 1), (10, 10), (40, 40), (99, 40), (0, 1), ("x", 20)],
    )
    def test_attribution_limit(self, captured: dict[str, Any], given: Any, expected: int) -> None:
        """默认 20、上限 40：界面能翻看任意一天的完整报告，不再只展开最新一份。"""
        _result("attribution", {"limit": given})
        assert captured["limit"] == expected

    def test_attribution_limit_not_capped_downstream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        IPC 层和 _query_attribution 各有一道钳制。只改一处会被另一处静默截断成
        10 —— 界面拿到的报告份数少于它请求的，而且没有任何报错。
        """
        from agents import history_tools as H

        seen: dict[str, Any] = {}

        def fake_load(limit: int, _ctx: Any) -> list[dict[str, Any]]:
            seen["limit"] = limit
            return []

        monkeypatch.setattr(H, "_load_attribution_rows", fake_load)
        H._query_attribution(40, None)
        assert seen["limit"] == 40, "下游把 40 截断了"

    def test_attribution_history_records_are_complete(self) -> None:
        """
        历史报告必须自带 policy_display / execution_summary / shadow ——
        界面靠它们渲染完整报告。只回摘要的话历史页签点开是空的。
        """
        from agents.history_tools import _attribution_record

        row = {
            "report_date": "2026-08-15",
            "window_start": "2026-06-16",
            "window_end": "2026-08-15",
            "policy_governor": {},
            "execution_state": {},
            "shadow_summary": {"runs": 7},
            "signal_actions": [],
        }
        record = _attribution_record(row)
        for key in ("policy_display", "execution_summary", "shadow", "signal_actions"):
            assert key in record, f"历史记录缺 {key}，页签点开会是空的"


class TestMarketRouting:
    """三个市场是三张表，切市场必须换表重查，不能在客户端过滤。"""

    @pytest.mark.parametrize(
        ("given", "expected"),
        [("cn", "cn"), ("us", "us"), ("hk", "hk"), ("US", "us"), ("  hk  ", "hk")],
    )
    def test_market_is_passed_through(self, captured: dict[str, Any], given: str, expected: str) -> None:
        _result("tracking", {"market": given})
        assert captured["market"] == expected

    @pytest.mark.parametrize("given", ["", None, "bogus", "cn; drop table", "recommendation_tracking_us"])
    def test_unknown_market_falls_back_to_cn(self, captured: dict[str, Any], given: Any) -> None:
        """认不出的市场退回 cn —— 绝不能拿它去拼表名。"""
        _result("tracking", {"market": given})
        assert captured["market"] == "cn"

    def test_default_is_cn(self, captured: dict[str, Any]) -> None:
        _result("tracking")
        assert captured["market"] == "cn"


class TestTableWhitelist:
    def test_only_three_tables(self) -> None:
        from integrations.supabase_recommendation import RECOMMENDATION_TABLES, recommendation_table

        assert set(RECOMMENDATION_TABLES) == {"cn", "us", "hk"}
        assert recommendation_table("us").endswith("_us")
        assert recommendation_table("hk").endswith("_hk")

    @pytest.mark.parametrize("evil", ["", "bogus", "cn'; --", "../other"])
    def test_never_builds_arbitrary_table_name(self, evil: str) -> None:
        """表名只能来自白名单映射，任何输入都不该拼出新表名。"""
        from integrations.supabase_recommendation import RECOMMENDATION_TABLES, recommendation_table

        assert recommendation_table(evil) in RECOMMENDATION_TABLES.values()


class TestLocalCacheIsolation:
    """本地 SQLite 只缓存 A 股；美股/港股既不能读它也不能写它。"""

    def test_non_cn_skips_local_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents import history_tools as H

        def boom(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
            raise AssertionError("美股不该读 A 股的本地缓存")

        monkeypatch.setattr(H, "_load_local_recommendations", boom)
        monkeypatch.setattr(H, "_load_remote_recommendations", lambda *_a, **_kw: [{"code": "AAPL.US"}])
        out = H._query_recommendation(10, None, market="us")
        assert out["market"] == "us"

    def test_non_cn_does_not_write_local_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents import history_tools as H

        wrote: list[Any] = []
        monkeypatch.setattr(H, "_cache_recommendations", lambda rows: wrote.append(rows))
        monkeypatch.setattr(
            "integrations.supabase_recommendation.load_recommendation_tracking",
            lambda **_kw: [{"code": "AAPL.US"}],
        )
        H._load_remote_recommendations(10, None, market="us")
        assert wrote == [], "美股行写进 A 股缓存会污染 A 股结果"

    def test_cn_still_writes_local_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents import history_tools as H

        wrote: list[Any] = []
        monkeypatch.setattr(H, "_cache_recommendations", lambda rows: wrote.append(rows))
        monkeypatch.setattr(
            "integrations.supabase_recommendation.load_recommendation_tracking",
            lambda **_kw: [{"code": "600519"}],
        )
        H._load_remote_recommendations(10, None, market="cn")
        assert len(wrote) == 1


class TestErrorSurfacing:
    """query_history 用返回值里的 error 表示失败；不能把它当成空结果。"""

    @pytest.mark.parametrize("method", ["tracking", "attribution"])
    def test_error_becomes_method_error(self, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
        class FakeSession:
            tool_context = None

        monkeypatch.setattr(
            "agents.history_tools.query_history",
            lambda **_kw: {"error": "supabase unreachable"},
        )
        monkeypatch.setattr("cli.ipc.session.get_session", lambda: FakeSession())

        with pytest.raises(MethodError) as excinfo:
            _events(method)
        assert "supabase unreachable" in str(excinfo.value)

    @pytest.mark.parametrize("method", ["tracking", "attribution"])
    def test_empty_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
        """真的没数据要正常返回空列表 —— 空态和失败必须能区分。"""

        class FakeSession:
            tool_context = None

        monkeypatch.setattr(
            "agents.history_tools.query_history",
            lambda **_kw: {"message": "暂无记录", "records": []},
        )
        monkeypatch.setattr("cli.ipc.session.get_session", lambda: FakeSession())

        out = _result(method)
        assert out.get("records") == []
        assert "error" not in out


class TestRegistration:
    @pytest.mark.parametrize("method", ["tracking", "attribution"])
    def test_registered(self, method: str) -> None:
        assert method in methods.METHODS
