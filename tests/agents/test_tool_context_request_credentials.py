"""请求级凭据模式：共用进程里凭据只能来自本次请求，绝不回落到 admin 客户端、本机配置或环境变量。"""

from __future__ import annotations

import os

import pytest

import agents.tool_context as tc
from agents.tool_context import (
    ToolContext,
    ensure_tushare_token,
    get_credential,
    has_cloud,
    has_request_credentials,
    resolve_llm_config,
)
from integrations import tushare_client


def _boom(*_args, **_kwargs):
    raise AssertionError("请求级凭据模式不该走到这里")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("TUSHARE_TOKEN", "TICKFLOW_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
    tushare_client.set_runtime_token("")


def _ctx(credentials, **state):
    return ToolContext(state={"user_id": "user-1", "credentials": credentials, **state})


def test_credentials_come_only_from_the_request(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "env-token")
    monkeypatch.setattr(tc, "load_user_credentials", _boom)
    monkeypatch.setattr("integrations.local_auth.load_config", _boom)

    ctx = _ctx({"tushare_token": "  request-token  "})

    assert get_credential(ctx, "tushare_token", "TUSHARE_TOKEN") == "request-token"


def test_a_missing_key_is_missing_not_looked_up_elsewhere(monkeypatch):
    monkeypatch.setenv("TICKFLOW_API_KEY", "env-key")
    monkeypatch.setattr(tc, "load_user_credentials", _boom)
    monkeypatch.setattr("integrations.local_auth.load_config", _boom)

    assert get_credential(_ctx({"tushare_token": "t"}), "tickflow_api_key", "TICKFLOW_API_KEY") == ""


def test_an_empty_credentials_dict_still_isolates(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "env-token")
    monkeypatch.setattr(tc, "load_user_credentials", _boom)

    ctx = _ctx({})

    assert has_request_credentials(ctx)
    assert get_credential(ctx, "tushare_token", "TUSHARE_TOKEN") == ""


def test_request_mode_counts_as_cloud_so_local_fallbacks_stay_off():
    assert has_cloud(_ctx({}))
    assert not has_cloud(ToolContext(state={"user_id": "local"}))
    assert not has_request_credentials(None)
    assert not has_request_credentials(ToolContext(state={"credentials": "not-a-dict"}))


def test_ensure_tushare_token_sets_the_request_token_but_never_the_process_env():
    ensure_tushare_token(_ctx({"tushare_token": "request-token"}))

    assert "TUSHARE_TOKEN" not in os.environ
    assert tushare_client._runtime_token.get() == "request-token"


def test_the_llm_config_is_read_from_the_request_only(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-gemini")
    monkeypatch.setattr(tc, "_try_local_llm_config", _boom)
    ctx = _ctx({"llm_provider": "deepseek", "llm_api_key": "sk-req", "llm_model": "m", "llm_base_url": "https://x/v1"})

    assert resolve_llm_config(ctx) == ("deepseek", "sk-req", "m", "https://x/v1")
    assert resolve_llm_config(_ctx({})) == ("gemini", "", "", "")


def test_without_request_credentials_the_existing_cloud_path_is_unchanged(monkeypatch):
    monkeypatch.setattr(tc, "load_user_credentials", lambda user_id: {"tushare_token": f"cloud-{user_id}"})
    ctx = ToolContext(state={"user_id": "user-1", "access_token": "jwt"})

    assert get_credential(ctx, "tushare_token", "TUSHARE_TOKEN") == "cloud-user-1"
    assert has_cloud(ctx)
