from __future__ import annotations

import logging
import os
import threading
import time
from hashlib import sha256
from typing import Any

logger = logging.getLogger(__name__)

LOCAL_USER_ID = "local"
CRED_CACHE_TTL = 300

_cred_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cred_cache_lock = threading.Lock()
_user_client_cache: dict[str, Any] = {}
_AUTH_ERR_KEYWORDS = ("invalid", "expired", "revoked", "refresh", "jwt", "token")


class ToolContext:
    """Shared tool context for Web, CLI, MCP, and sub-agent execution."""

    def __init__(self, state=None):
        self.state = state or {}
        self.provider = None
        self.registry = None
        self.on_progress = None
        self.cancel_check = None
        # 并发安全工具（如 analyze_stock）会在线程池中同时读改写 state 的
        # 同一个 key（如 last_stock_diagnosis 的合并列表），需要锁保护临界区，
        # 否则会出现 lost update：后完成的线程覆盖先完成线程写入的记录。
        self.state_lock = threading.Lock()


def load_user_credentials(user_id: str) -> dict[str, Any]:
    if not user_id:
        return {}

    now = time.monotonic()
    with _cred_cache_lock:
        cached = _cred_cache.get(user_id)
        if cached and (now - cached[0]) < CRED_CACHE_TTL:
            return cached[1]

    try:
        from integrations.supabase_portfolio import load_user_settings_admin

        row = load_user_settings_admin(user_id) or {}
    except Exception as e:
        logger.warning("load_user_credentials failed for %s: %s", user_id, e)
        row = {}

    with _cred_cache_lock:
        _cred_cache[user_id] = (time.monotonic(), row)
    return row


def get_user_id(tool_context: ToolContext | None = None) -> str:
    if tool_context is not None:
        uid = tool_context.state.get("user_id", "")
        if uid:
            return str(uid)
    return LOCAL_USER_ID


def has_cloud(tool_context: ToolContext | None) -> bool:
    return bool(tool_context and tool_context.state.get("access_token", ""))


def get_credential(tool_context: ToolContext | None, key: str, env_fallback: str = "") -> str:
    user_id = get_user_id(tool_context)
    if user_id:
        value = str(load_user_credentials(user_id).get(key, "") or "").strip()
        if value:
            return value
    if has_cloud(tool_context):
        return ""
    try:
        from integrations.local_auth import load_config

        local_value = str(load_config().get(key, "") or "").strip()
        if local_value:
            return local_value
    except Exception:
        logger.debug("failed to load credential '%s' from local config", key, exc_info=True)
    return os.getenv(env_fallback, "").strip() if env_fallback else ""


def resolve_llm_config(tool_context: ToolContext | None) -> tuple[str, str, str, str]:
    if not has_cloud(tool_context):
        try:
            local = _local_default_llm_config()
            if local:
                return local
        except Exception:
            logger.debug("failed to load LLM provider config from local config", exc_info=True)
    api_key = get_credential(tool_context, "gemini_api_key", "GEMINI_API_KEY")
    model = get_credential(tool_context, "gemini_model", "GEMINI_MODEL") or "gemini-2.0-flash"
    base_url = get_credential(tool_context, "gemini_base_url", "")
    return "gemini", api_key, model, base_url


def _local_default_llm_config() -> tuple[str, str, str, str] | None:
    from integrations._llm_types import OPENAI_COMPATIBLE_BASE_URLS
    from integrations.local_auth import load_default_model_id, load_model_configs

    configs = load_model_configs()
    default_id = load_default_model_id()
    cfg = next((item for item in configs if item["id"] == default_id), None)
    if not cfg or not cfg.get("api_key"):
        return None
    provider = cfg.get("provider_name", "openai")
    base_url = cfg.get("base_url", "") or OPENAI_COMPATIBLE_BASE_URLS.get(provider, "")
    return provider, cfg["api_key"], cfg.get("model", ""), base_url


def ensure_tushare_token(tool_context: ToolContext | None) -> None:
    token = get_credential(tool_context, "tushare_token", "TUSHARE_TOKEN")
    if token and not has_cloud(tool_context):
        os.environ["TUSHARE_TOKEN"] = token
    if tool_context is not None:
        tool_context.state["tushare_token"] = token
    from integrations.tushare_client import set_runtime_token

    set_runtime_token(token)


def get_user_client(tool_context: ToolContext | None):
    if tool_context is None:
        return None
    access_token = tool_context.state.get("access_token") or ""
    if not access_token:
        return None
    user_id = get_user_id(tool_context)
    cache_key = _user_client_cache_key(user_id, access_token)
    cached = _user_client_cache.get(cache_key)
    if cached is not None:
        return cached
    client, new_access, new_refresh = _create_or_relogin_client(tool_context, access_token)
    if client is None:
        return None
    if new_access:
        tool_context.state["access_token"] = new_access
    if new_refresh:
        tool_context.state["refresh_token"] = new_refresh
    if new_access or new_refresh:
        _persist_tool_session(tool_context)
    final_key = _user_client_cache_key(get_user_id(tool_context), new_access or access_token)
    _evict_stale_clients(final_key)
    _user_client_cache[final_key] = client
    return client


def with_auth_retry(tool_context: ToolContext | None, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        auth_msg = _auth_failure_message(result)
        if not auth_msg:
            return result
        raise RuntimeError(auth_msg)
    except Exception as e:
        if not _is_auth_error(e) or tool_context is None:
            raise
    close_cached_clients()
    client, _new_at, _new_rt = _relogin_and_create_client(tool_context)
    if client is None:
        return None
    args = _replace_client_arg(args, kwargs, client)
    return fn(*args, **kwargs)


def is_auth_failure_result(result: Any) -> bool:
    return bool(_auth_failure_message(result))


def close_cached_clients() -> None:
    from integrations.supabase_base import close_client

    for client in _user_client_cache.values():
        close_client(client)
    _user_client_cache.clear()


def _user_client_cache_key(user_id: str, access_token: str) -> str:
    digest = sha256(str(access_token).encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{user_id}:{digest}"


def _create_or_relogin_client(tool_context: ToolContext, access_token: str):
    refresh_token = tool_context.state.get("refresh_token") or ""
    from integrations.supabase_base import create_user_client, get_session_tokens

    try:
        client = create_user_client(access_token, refresh_token)
        new_access, new_refresh = get_session_tokens(client)
        return client, new_access, new_refresh
    except Exception as e:
        if not _is_auth_error(e):
            raise
    return _relogin_and_create_client(tool_context)


def _evict_stale_clients(keep_key: str) -> None:
    from integrations.supabase_base import close_client

    stale = [key for key in _user_client_cache if key != keep_key]
    for key in stale:
        close_client(_user_client_cache.pop(key))


def _persist_tool_session(tool_context: ToolContext | None) -> None:
    if tool_context is None:
        return
    state = tool_context.state
    if not state.get("access_token") or not state.get("refresh_token"):
        return
    try:
        from integrations.local_auth import load_session, save_session

        data = load_session() or {}
        for key in ("user_id", "email", "access_token", "refresh_token"):
            value = state.get(key)
            if value:
                data[key] = value
        save_session(data)
    except Exception:
        logger.debug("failed to persist refreshed Supabase session", exc_info=True)


def _relogin_and_create_client(tool_context: ToolContext | None):
    from integrations.local_auth import auto_relogin

    data = auto_relogin()
    if not data or tool_context is None:
        return None, "", ""
    tool_context.state["user_id"] = data.get("user_id", tool_context.state.get("user_id", ""))
    tool_context.state["email"] = data.get("email", tool_context.state.get("email", ""))
    tool_context.state["access_token"] = data["access_token"]
    tool_context.state["refresh_token"] = data["refresh_token"]
    from integrations.supabase_base import create_user_client, get_session_tokens

    client = create_user_client(data["access_token"], data["refresh_token"])
    new_access, new_refresh = get_session_tokens(client)
    if new_access:
        tool_context.state["access_token"] = new_access
    if new_refresh:
        tool_context.state["refresh_token"] = new_refresh
    _persist_tool_session(tool_context)
    return client, new_access or data["access_token"], new_refresh or data["refresh_token"]


def _is_auth_error(e: Exception) -> bool:
    err = str(e).lower()
    return any(key in err for key in _AUTH_ERR_KEYWORDS)


def _auth_failure_message(result: Any) -> str:
    if getattr(result, "ok", None) is False and hasattr(result, "message"):
        msg = str(result.message or "")
        return msg if _is_auth_error(Exception(msg)) else ""
    if isinstance(result, tuple) and len(result) >= 2 and result[0] is False:
        msg = str(result[1] or "")
        return msg if _is_auth_error(Exception(msg)) else ""
    if isinstance(result, dict) and result.get("error"):
        msg = str(result.get("error") or "")
        return msg if _is_auth_error(Exception(msg)) else ""
    return ""


def _replace_client_arg(args: tuple, kwargs: dict[str, Any], client) -> tuple:
    if "client" in kwargs:
        kwargs["client"] = client
        return args
    args_list = list(args)
    for index, value in enumerate(args_list):
        if hasattr(value, "auth") and hasattr(value, "postgrest"):
            args_list[index] = client
            return tuple(args_list)
    return args
