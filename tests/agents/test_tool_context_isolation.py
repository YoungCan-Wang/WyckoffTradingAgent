from __future__ import annotations

import os

from agents import tool_context


def test_cloud_credentials_do_not_fall_back_to_local_or_environment(monkeypatch):
    ctx = tool_context.ToolContext({"user_id": "user-a", "access_token": "jwt"})
    monkeypatch.setattr(tool_context, "load_user_credentials", lambda _uid: {})
    monkeypatch.setattr("integrations.local_auth.load_config", lambda: {"tushare_token": "operator-local"})
    monkeypatch.setenv("TUSHARE_TOKEN", "operator-env")

    assert tool_context.get_credential(ctx, "tushare_token", "TUSHARE_TOKEN") == ""


def test_cloud_llm_config_falls_back_to_local_model_when_cloud_has_no_key(monkeypatch):
    """LLM key 是本地 CLI 自用配置，云端没配时必须回落，否则登录即用不了策略/研报工具。

    与 tushare_token 的隔离规则不同：Python agent 只在本机 CLI / mcp_server 跑，
    读的是同一用户的 wyckoff.json，不存在跨租户泄漏面。
    """
    ctx = tool_context.ToolContext({"user_id": "user-a", "access_token": "jwt"})
    monkeypatch.setattr(tool_context, "load_user_credentials", lambda _uid: {})
    monkeypatch.setattr(
        tool_context,
        "_local_default_llm_config",
        lambda: ("openai", "local-key", "gpt-test", "https://example.invalid"),
    )

    provider, api_key, model, base_url = tool_context.resolve_llm_config(ctx)

    assert (provider, api_key, model, base_url) == ("openai", "local-key", "gpt-test", "https://example.invalid")


def test_cloud_llm_config_prefers_cloud_key_over_local(monkeypatch):
    ctx = tool_context.ToolContext({"user_id": "user-a", "access_token": "jwt"})
    monkeypatch.setattr(tool_context, "load_user_credentials", lambda _uid: {"gemini_api_key": "cloud-key"})
    monkeypatch.setattr(tool_context, "_local_default_llm_config", lambda: ("openai", "local-key", "gpt-test", ""))

    provider, api_key, _model, _base_url = tool_context.resolve_llm_config(ctx)

    assert (provider, api_key) == ("gemini", "cloud-key")


def test_cloud_tushare_token_is_context_local(monkeypatch):
    ctx = tool_context.ToolContext({"user_id": "user-a", "access_token": "jwt"})
    monkeypatch.setattr(tool_context, "load_user_credentials", lambda _uid: {"tushare_token": "user-token"})
    monkeypatch.setenv("TUSHARE_TOKEN", "operator-token")

    tool_context.ensure_tushare_token(ctx)

    assert os.environ["TUSHARE_TOKEN"] == "operator-token"
    assert ctx.state["tushare_token"] == "user-token"
    from integrations.tushare_client import has_tushare_token

    assert has_tushare_token()
