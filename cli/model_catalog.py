"""OpenRouter model catalog: real context windows instead of name-pattern guesses.

``cli.model_metadata.infer_context_window`` 按模型名正则猜窗口，每接一个新模型都要
改一次正则表，且猜错的代价不对称：猜小会让压缩过早触发（实测 poolside/nemotron 全部
落到 64k 默认值，而真实窗口是 262k/1M，压缩在窗口只用了 5% 时就开始丢历史）。

OpenRouter 的 ``/models`` 已经报了每个模型的 ``context_length``，直接取真值。结果落盘
缓存，避免每次启动都发网络请求；取不到时回退正则表，不阻塞启动。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_PATH = Path.home() / ".wyckoff" / "model_catalog.json"
CACHE_TTL_SECONDS = 7 * 24 * 3600
FETCH_TIMEOUT_SECONDS = 10.0


def looks_like_openrouter(base_url: str) -> bool:
    return "openrouter.ai" in str(base_url or "").lower()


def catalog_context_window(model_name: str, *, allow_fetch: bool = True) -> int | None:
    """Real context window for an OpenRouter model id, or None when unknown."""
    model = str(model_name or "").strip()
    if not model:
        return None
    catalog = load_catalog(allow_fetch=allow_fetch)
    window = catalog.get(model)
    if window:
        return window
    # ``:free`` / ``:batch`` 等变体的窗口可能与基础模型不同，所以先精确匹配；
    # 仅当变体缺失时才退到基础模型 id。
    base = model.split(":", 1)[0]
    return catalog.get(base)


def load_catalog(*, allow_fetch: bool = True) -> dict[str, int]:
    cached = _read_cache()
    if cached is not None:
        return cached
    if not allow_fetch:
        return {}
    fetched = fetch_catalog()
    if fetched:
        _write_cache(fetched)
    return fetched


def refresh_catalog() -> dict[str, int]:
    """Force a fetch and persist it, bypassing the TTL. Returns {} on failure."""
    fetched = fetch_catalog()
    if fetched:
        _write_cache(fetched)
    return fetched


def fetch_catalog() -> dict[str, int]:
    """Fetch id -> context_length from OpenRouter. Returns {} on any failure."""
    try:
        import httpx

        response = httpx.get(OPENROUTER_MODELS_URL, timeout=FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        logger.debug("openrouter model catalog fetch failed", exc_info=True)
        return {}
    return _parse_catalog(payload)


def _parse_catalog(payload: Any) -> dict[str, int]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or "").strip()
        window = row.get("context_length")
        try:
            window_int = int(window)
        except (TypeError, ValueError):
            continue
        if model_id and window_int > 0:
            out[model_id] = window_int
    return out


def _read_cache() -> dict[str, int] | None:
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        fetched_at = float(raw.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return None
    if time.time() - fetched_at > CACHE_TTL_SECONDS:
        return None
    windows = raw.get("windows")
    if not isinstance(windows, dict):
        return None
    return {str(k): int(v) for k, v in windows.items() if _positive_int(v)}


def _write_cache(windows: dict[str, int]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"fetched_at": time.time(), "windows": windows}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("failed to persist model catalog cache", exc_info=True)


def _positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


__all__ = [
    "CACHE_PATH",
    "catalog_context_window",
    "fetch_catalog",
    "load_catalog",
    "looks_like_openrouter",
    "refresh_catalog",
]
