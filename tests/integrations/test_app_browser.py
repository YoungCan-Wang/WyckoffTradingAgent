"""应用内置浏览器的 Agent 侧驱动。

重点：SSRF 防线（目标 URL 来自模型）、动作白名单、以及在非桌面环境下必须
明确报不可用而不是静默失败。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents.app_browser_tools import app_browser
from integrations import app_browser as ab


@pytest.fixture
def connected(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """假装运行在桌面应用里，并记录发出的请求。"""
    monkeypatch.setenv(ab.ENV_URL, "http://127.0.0.1:9999")
    monkeypatch.setenv(ab.ENV_TOKEN, "secret-token")
    sent: list[dict[str, Any]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def fake_urlopen(request: Any, timeout: int = 0) -> FakeResponse:
        body = json.loads(request.data.decode("utf-8"))
        sent.append({"body": body, "headers": dict(request.headers)})
        action = body["action"]
        if action == "text":
            return FakeResponse({"ok": True, "result": {"text": "页面正文", "url": "https://example.com/"}})
        if action == "navigate":
            return FakeResponse({"ok": True, "result": {"url": body["params"]["url"], "title": "标题"}})
        return FakeResponse({"ok": True, "result": {"done": action}})

    monkeypatch.setattr(ab.urllib.request, "urlopen", fake_urlopen)
    return sent


class TestAvailability:
    def test_unavailable_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ab.ENV_URL, raising=False)
        monkeypatch.delenv(ab.ENV_TOKEN, raising=False)
        assert ab.is_available() is False
        result = app_browser("navigate", url="https://example.com")
        assert "只能在 Wyckoff 桌面应用" in result["error"]
        # 明确指路到 CLI 可用的替代工具，而不是让模型反复重试。
        assert "browser_research" in result["hint"]

    def test_requires_both_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ab.ENV_URL, "http://127.0.0.1:1")
        monkeypatch.delenv(ab.ENV_TOKEN, raising=False)
        assert ab.is_available() is False


class TestSsrfGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # 云元数据
            "http://127.0.0.1/admin",
            "http://localhost:8080/",
            "http://10.0.0.5/internal",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://user:pw@example.com/",
        ],
    )
    def test_blocks_dangerous_urls(self, connected: list[dict[str, Any]], url: str) -> None:
        result = ab.call("navigate", url=url)
        assert "error" in result, f"{url} 应被拦截"
        # 关键：拦截发生在发请求之前，Electron 侧不该收到任何东西。
        assert connected == []

    def test_allows_public_https(self, connected: list[dict[str, Any]]) -> None:
        result = ab.call("navigate", url="https://example.com/page")
        assert "error" not in result
        assert connected[0]["body"]["params"]["url"] == "https://example.com/page"


class TestActionWhitelist:
    def test_rejects_unknown_action(self, connected: list[dict[str, Any]]) -> None:
        result = ab.call("evaluate", code="fetch('/x')")
        assert "不支持的动作" in result["error"]
        assert connected == []

    def test_tool_rejects_unknown_verb(self, connected: list[dict[str, Any]]) -> None:
        assert "不支持的 action" in app_browser("execute")["error"]

    def test_click_requires_selector(self, connected: list[dict[str, Any]]) -> None:
        assert "需要 selector" in app_browser("click")["error"]

    def test_navigate_requires_url(self, connected: list[dict[str, Any]]) -> None:
        assert "需要 url" in app_browser("navigate")["error"]


class TestRequests:
    def test_token_is_sent_as_header(self, connected: list[dict[str, Any]]) -> None:
        ab.call("title")
        headers = {k.lower(): v for k, v in connected[0]["headers"].items()}
        assert headers["x-wyckoff-token"] == "secret-token"

    def test_navigate_returns_page_text(self, connected: list[dict[str, Any]]) -> None:
        """导航后顺带回正文，省一次往返。"""
        result = app_browser("navigate", url="https://example.com")
        assert result["title"] == "标题"
        assert result["text"] == "页面正文"
        assert [c["body"]["action"] for c in connected] == ["navigate", "text"]

    def test_read_maps_to_text_action(self, connected: list[dict[str, Any]]) -> None:
        result = app_browser("read")
        assert result["text"] == "页面正文"
        assert connected[0]["body"]["action"] == "text"

    def test_fill_passes_value(self, connected: list[dict[str, Any]]) -> None:
        app_browser("fill", selector="#q", value="茅台")
        assert connected[0]["body"]["params"] == {"selector": "#q", "value": "茅台"}

    def test_wait_is_clamped_to_int(self, connected: list[dict[str, Any]]) -> None:
        app_browser("wait", ms="900")
        assert connected[0]["body"]["params"]["ms"] == 900

    def test_error_payload_is_surfaced(self, connected: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
        """Electron 报的失败要原样带回，不能吞成空结果。"""

        class FailResponse:
            def read(self) -> bytes:
                return json.dumps({"ok": False, "error": "no element matched #x"}).encode()

            def __enter__(self) -> FailResponse:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

        monkeypatch.setattr(ab.urllib.request, "urlopen", lambda *a, **k: FailResponse())
        assert ab.call("click", selector="#x")["error"] == "no element matched #x"


class TestRedaction:
    def test_page_text_is_redacted(self, connected: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
        """页面正文可能含密钥样式的串，落进模型上下文前要脱敏。"""

        class KeyResponse:
            def read(self) -> bytes:
                leak = "token sk-abcdefghijklmnopqrstuvwxyz0123456789 end"
                return json.dumps({"ok": True, "result": {"text": leak, "url": "u"}}).encode()

            def __enter__(self) -> KeyResponse:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

        monkeypatch.setattr(ab.urllib.request, "urlopen", lambda *a, **k: KeyResponse())
        text = ab.read_page()["text"]
        assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in text


class TestToolRegistration:
    def test_registered_in_schemas(self) -> None:
        from cli.tools import TOOL_SCHEMAS

        entry = next((t for t in TOOL_SCHEMAS if t["name"] == "app_browser"), None)
        assert entry is not None
        assert "action" in entry["parameters"]["required"]
