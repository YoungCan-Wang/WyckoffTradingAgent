"""Local Supabase auth and model configuration storage."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from integrations.supabase_public_config import SUPABASE_ANON_KEY, SUPABASE_ANON_URL

logger = logging.getLogger(__name__)

SESSION_DIR = Path.home() / ".wyckoff"
SESSION_FILE = SESSION_DIR / "session.json"
CONFIG_FILE = SESSION_DIR / "wyckoff.json"
_OLD_CONFIG_FILE = SESSION_DIR / "config.json"
_CONFIG_THREAD_LOCK = threading.RLock()
_SESSION_THREAD_LOCK = threading.RLock()


def save_session(data: dict[str, Any]) -> None:
    with _write_lock(SESSION_FILE, _SESSION_THREAD_LOCK):
        _atomic_write_json(SESSION_FILE, data)


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
    """退出登录：清 session **并**清掉自动重登凭据。

    只删 session 文件是不够的：`login()` 会把邮箱和明文密码写进 config 供
    `auto_relogin()` 用，而 `agents/tool_context.py` 在 token 失效时会调它 ——
    于是「退出登录」之后的下一次工具调用会把用户**静默登回去**（实测复现）。

    用户点退出就是要退出。所以这里把凭据一起清掉，即便代价是 CLI / daemon
    在显式退出后也需要重新登录 —— 那比「退了但没退」正确。

    注意 `auto_relogin` 仍然保留：它是 token 过期时的续期路径，不是登录态的
    来源。清掉凭据只让它拿不到东西，不会改变续期逻辑本身。
    """
    clear_session()
    # 两个键一起清：只清 password 会留一个孤立的 email，下次登录界面预填一个
    # 无法自动登录的邮箱，比什么都不留更让人困惑。
    for key in ("email", "password"):
        try:
            save_config_key(key, "")
        except OSError:
            logger.warning("failed to clear stored credential: %s", key, exc_info=True)


def clear_session() -> None:
    with _write_lock(SESSION_FILE, _SESSION_THREAD_LOCK):
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
    def update(data: dict[str, Any]) -> None:
        models = data.get("models", [])
        for index, model in enumerate(models):
            if model["id"] == entry["id"]:
                models[index] = entry
                break
        else:
            models.append(entry)
        data["models"] = models
        if not data.get("default") or not any(model["id"] == data["default"] for model in models):
            data["default"] = models[0]["id"]

    _update_config(update)


def remove_model_entry(model_id: str) -> bool:
    removed = False

    def update(data: dict[str, Any]) -> bool:
        nonlocal removed
        models = data.get("models", [])
        if len(models) <= 1 or not any(model["id"] == model_id for model in models):
            return False
        data["models"] = [model for model in models if model["id"] != model_id]
        if data.get("default") == model_id:
            data["default"] = data["models"][0]["id"] if data["models"] else ""
        removed = True
        return True

    _update_config(update)
    return removed


def set_default_model(model_id: str) -> None:
    def update(data: dict[str, Any]) -> bool:
        if not any(model["id"] == model_id for model in data.get("models", [])):
            return False
        data["default"] = model_id
        return True

    _update_config(update)


def load_fallback_model_id() -> str:
    data = _ensure_models_format(load_config())
    fallback = data.get("fallback", "")
    models = data.get("models", [])
    if fallback and any(model["id"] == fallback for model in models):
        return fallback
    return ""


def set_fallback_model(model_id: str) -> None:
    def update(data: dict[str, Any]) -> bool:
        if model_id and not any(model["id"] == model_id for model in data.get("models", [])):
            return False
        data["fallback"] = model_id
        return True

    _update_config(update)


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
    if key in TIMEOUT_CONFIG_SPECS:
        value = coerce_timeout_config_value(key, value)

    def update(data: dict[str, Any]) -> None:
        data[key] = value

    _update_config(update)


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


def _update_config(update: Callable[[dict[str, Any]], bool | None]) -> None:
    """在同一把跨进程锁内完成 read-modify-write，避免多个桌面实例互相覆盖。"""
    with _write_lock(CONFIG_FILE, _CONFIG_THREAD_LOCK):
        data = _ensure_models_format(_load_config_for_update())
        if update(data) is False:
            return
        _atomic_write_json(CONFIG_FILE, data, indent=2)


def _load_config_for_update() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # 读失败时绝不能用一个空对象覆盖原文件；保留现场供用户恢复。
        raise RuntimeError(f"配置文件无法读取，拒绝覆盖: {CONFIG_FILE}") from exc


@contextmanager
def _write_lock(path: Path, thread_lock: threading.RLock) -> Iterator[None]:
    """进程内用 RLock，进程间用系统文件锁；锁文件本身不承载配置内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with thread_lock, lock_path.open("a+b") as lock_file:
        _lock_file(lock_file)
        try:
            yield
        finally:
            _unlock_file(lock_file)


def _lock_file(lock_file: Any) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        if not lock_file.read(1):
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_file(lock_file: Any) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: dict[str, Any], *, indent: int | None = None) -> None:
    """同目录临时文件落盘后原子替换，读者只会看到旧版或完整新版。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _ensure_models_format(data: dict[str, Any]) -> dict[str, Any]:
    if "models" in data:
        return data
    if data.get("provider_name") and data.get("api_key"):
        return _migrate_config(data)
    return data


def _migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "id": data.get("provider_name", "default"),
        "provider_name": data["provider_name"],
        "api_key": data["api_key"],
        "model": data.get("model", ""),
        "base_url": data.get("base_url", ""),
    }
    migrated = {
        key: value for key, value in data.items() if key not in {"provider_name", "api_key", "model", "base_url"}
    }
    migrated.update({"models": [entry], "default": entry["id"]})
    return migrated
