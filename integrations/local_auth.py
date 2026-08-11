"""Local Supabase auth and model configuration storage."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from integrations.supabase_public_config import SUPABASE_ANON_KEY, SUPABASE_ANON_URL

logger = logging.getLogger(__name__)

SESSION_DIR = Path.home() / ".wyckoff"
SESSION_FILE = SESSION_DIR / "session.json"
CONFIG_FILE = SESSION_DIR / "wyckoff.json"
_OLD_CONFIG_FILE = SESSION_DIR / "config.json"


def save_session(data: dict[str, Any]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_session() -> dict[str, Any] | None:
    if not SESSION_FILE.exists():
        return None
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def login(email: str, password: str) -> dict[str, Any]:
    client = _create_client()
    resp = client.auth.sign_in_with_password({"email": email, "password": password})
    data = {
        "user_id": resp.user.id,
        "email": resp.user.email,
        "access_token": resp.session.access_token,
        "refresh_token": resp.session.refresh_token,
    }
    save_session(data)
    save_config_key("email", email)
    save_config_key("password", password)
    return data


def auto_relogin() -> dict[str, Any] | None:
    cfg = load_config()
    email = str(cfg.get("email", "") or "").strip()
    password = str(cfg.get("password", "") or "").strip()
    if not email or not password:
        return None
    try:
        return login(email, password)
    except Exception:
        logger.debug("auto re-login failed", exc_info=True)
        return None


def restore_session() -> dict[str, Any] | None:
    data = load_session()
    if not data or not data.get("access_token") or not data.get("refresh_token"):
        return auto_relogin()
    try:
        client = _create_client()
        client.auth.set_session(data["access_token"], data["refresh_token"])
        user_resp = client.auth.get_user()
        if not user_resp or not user_resp.user:
            clear_session()
            return auto_relogin()
        session = client.auth.get_session()
        if session:
            data["access_token"] = session.access_token
            data["refresh_token"] = session.refresh_token
            save_session(data)
        return data
    except Exception as exc:
        logger.debug("session restore failed", exc_info=True)
        if _invalid_session_error(exc):
            clear_session()
            return auto_relogin()
        return data


def logout() -> None:
    clear_session()


def clear_session() -> None:
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except OSError:
        logger.warning("failed to clear session file", exc_info=True)


def load_model_configs() -> list[dict[str, Any]]:
    data = _ensure_models_format(load_config())
    return data.get("models", [])


def load_default_model_id() -> str | None:
    data = _ensure_models_format(load_config())
    models = data.get("models", [])
    default = data.get("default", "")
    if default and any(model["id"] == default for model in models):
        return default
    return models[0]["id"] if models else None


def save_model_entry(entry: dict[str, Any]) -> None:
    data = _ensure_models_format(load_config())
    models = data.get("models", [])
    found = False
    for index, model in enumerate(models):
        if model["id"] == entry["id"]:
            models[index] = entry
            found = True
            break
    if not found:
        models.append(entry)
    data["models"] = models
    if not data.get("default") or not any(model["id"] == data["default"] for model in models):
        data["default"] = models[0]["id"]
    _save_config(data)


def remove_model_entry(model_id: str) -> bool:
    data = _ensure_models_format(load_config())
    models = data.get("models", [])
    if len(models) <= 1:
        return False
    data["models"] = [model for model in models if model["id"] != model_id]
    if data.get("default") == model_id:
        data["default"] = data["models"][0]["id"] if data["models"] else ""
    _save_config(data)
    return True


def set_default_model(model_id: str) -> None:
    data = _ensure_models_format(load_config())
    models = data.get("models", [])
    if any(model["id"] == model_id for model in models):
        data["default"] = model_id
        _save_config(data)


def load_fallback_model_id() -> str:
    data = _ensure_models_format(load_config())
    fallback = data.get("fallback", "")
    models = data.get("models", [])
    if fallback and any(model["id"] == fallback for model in models):
        return fallback
    return ""


def set_fallback_model(model_id: str) -> None:
    data = _ensure_models_format(load_config())
    if model_id:
        models = data.get("models", [])
        if not any(model["id"] == model_id for model in models):
            return
    data["fallback"] = model_id
    _save_config(data)


def save_model_config(config: dict[str, Any]) -> None:
    entry = dict(config)
    if "id" not in entry:
        entry["id"] = entry.get("provider_name", "default")
    save_model_entry(entry)


def load_model_config() -> dict[str, Any] | None:
    configs = load_model_configs()
    if not configs:
        return None
    default_id = load_default_model_id()
    return next((model for model in configs if model["id"] == default_id), configs[0])


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists() and _OLD_CONFIG_FILE.exists():
        try:
            _OLD_CONFIG_FILE.rename(CONFIG_FILE)
        except OSError:
            logger.warning("config migration rename failed", exc_info=True)
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_config_key(key: str, value: Any) -> None:
    data = load_config()
    if key in TIMEOUT_CONFIG_SPECS:
        value = coerce_timeout_config_value(key, value)
    data[key] = value
    _save_config(data)


# 模型空闲/首 token 超时与工具执行超时（秒）。控制面板 / TUI / CLI 均可改。
DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS = 120
DEFAULT_TOOL_TIMEOUT_SECONDS = 60
TIMEOUT_CONFIG_SPECS: dict[str, tuple[int, int, int]] = {
    # key: (default, min, max)
    "stream_chunk_timeout_seconds": (DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS, 10, 600),
    "tool_timeout_seconds": (DEFAULT_TOOL_TIMEOUT_SECONDS, 5, 300),
}


def coerce_timeout_config_value(key: str, value: Any) -> int:
    """Validate and clamp a timeout config value; raises ValueError on bad input."""
    if key not in TIMEOUT_CONFIG_SPECS:
        raise ValueError(f"unknown timeout key: {key}")
    default, min_v, max_v = TIMEOUT_CONFIG_SPECS[key]
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数秒") from exc
    if parsed < min_v or parsed > max_v:
        raise ValueError(f"{key} 须在 {min_v}–{max_v} 秒之间")
    return parsed


def get_stream_chunk_timeout_seconds(config: dict[str, Any] | None = None) -> float:
    return float(_get_timeout_seconds("stream_chunk_timeout_seconds", config))


def get_tool_timeout_seconds(config: dict[str, Any] | None = None) -> float:
    return float(_get_timeout_seconds("tool_timeout_seconds", config))


def timeout_config_defaults() -> dict[str, int]:
    return {key: spec[0] for key, spec in TIMEOUT_CONFIG_SPECS.items()}


def _get_timeout_seconds(key: str, config: dict[str, Any] | None = None) -> int:
    default, min_v, max_v = TIMEOUT_CONFIG_SPECS[key]
    cfg = config if config is not None else load_config()
    raw = cfg.get(key, default)
    try:
        parsed = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(min_v, min(max_v, parsed))


def _create_client():
    from supabase import create_client

    return create_client(SUPABASE_ANON_URL, SUPABASE_ANON_KEY)


def _invalid_session_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "invalid" in text or "expired" in text or "revoked" in text


def _save_config(data: dict[str, Any]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_models_format(data: dict[str, Any]) -> dict[str, Any]:
    if "models" in data:
        return data
    if data.get("provider_name") and data.get("api_key"):
        migrated = _migrate_config(data)
        _save_config(migrated)
        return migrated
    return data


def _migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "id": data.get("provider_name", "default"),
        "provider_name": data["provider_name"],
        "api_key": data["api_key"],
        "model": data.get("model", ""),
        "base_url": data.get("base_url", ""),
    }
    return {"models": [entry], "default": entry["id"]}
