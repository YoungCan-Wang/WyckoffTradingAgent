from __future__ import annotations

from types import SimpleNamespace

from utils import markdown_webhooks, telegram


def test_split_telegram_message_splits_long_lines() -> None:
    chunks = telegram.split_telegram_message("abc\n" + "x" * 12, max_len=5)

    assert chunks == ["abc", "xxxxx", "xxxxx", "xx"]


def test_send_to_telegram_posts_numbered_chunks(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.delenv("PROXY_URL", raising=False)
    monkeypatch.setattr(telegram, "append_tickflow_limit_hint", lambda text: str(text or ""))

    def fake_post(url, *, json, timeout, proxies):
        calls.append({"url": url, "json": json, "timeout": timeout, "proxies": proxies})
        return SimpleNamespace(status_code=200, text="", json=lambda: {"ok": True})

    monkeypatch.setattr(telegram.requests, "post", fake_post)

    assert telegram.send_to_telegram("x" * 3901, tg_bot_token="token", tg_chat_id="chat")
    assert [call["json"]["text"].split("\n", 1)[0] for call in calls] == ["[1/2]", "[2/2]"]
    assert {call["url"] for call in calls} == {"https://api.telegram.org/bottoken/sendMessage"}
    assert all(call["proxies"] is None for call in calls)


def test_enterprise_markdown_webhooks_shape_payload(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(markdown_webhooks, "append_tickflow_limit_hint", lambda text: str(text or ""))

    def fake_post(url, *, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return SimpleNamespace(status_code=200, text="", json=lambda: {"errcode": 0})

    monkeypatch.setattr(markdown_webhooks.requests, "post", fake_post)

    assert markdown_webhooks.send_wecom_notification("https://wecom.example", "标题", "正文")
    assert markdown_webhooks.send_dingtalk_notification("https://ding.example", "标题", "正文")

    assert calls[0]["json"] == {"msgtype": "markdown", "markdown": {"content": "# 标题\n\n正文"}}
    assert calls[1]["json"] == {"msgtype": "markdown", "markdown": {"title": "标题", "text": "# 标题\n\n正文"}}
