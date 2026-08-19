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

    def fake_query(source: str, limit: int = 20, tool_context: Any = None, **_kw: Any) -> dict[str, Any]:
        seen["source"] = source
        seen["limit"] = limit
        seen["tool_context"] = tool_context
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
        [(None, 30), (1, 1), (50, 50), (999, 50), (0, 1), (-5, 1), ("abc", 30)],
    )
    def test_tracking_limit(self, captured: dict[str, Any], given: Any, expected: int) -> None:
        _result("tracking", {"limit": given})
        assert captured["limit"] == expected

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(None, 5), (1, 1), (10, 10), (99, 10), (0, 1), ("x", 5)],
    )
    def test_attribution_limit(self, captured: dict[str, Any], given: Any, expected: int) -> None:
        _result("attribution", {"limit": given})
        assert captured["limit"] == expected


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
