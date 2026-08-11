"""共享运行时初始化 — TUI 和无界面执行都从这里取 provider 与本地服务。"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def init_local_services() -> None:
    try:
        from integrations.local_db import init_db, prune_memories

        init_db()
        prune_memories()
        from integrations.sync import sync_all_background

        sync_all_background()
    except Exception:
        logger.warning("local db init or sync failed", exc_info=True)


def load_config_env() -> None:
    from cli.auth import load_config

    cfg = load_config()
    for key, env_key in [("tushare_token", "TUSHARE_TOKEN"), ("tickflow_api_key", "TICKFLOW_API_KEY")]:
        value = str(cfg.get(key, "") or "").strip()
        if value:
            os.environ.setdefault(env_key, value)


def seed_model_env(configs: list[dict[str, Any]]) -> None:
    env_map = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
    for cfg in configs:
        env_key = env_map.get(cfg.get("provider_name", ""))
        if env_key and cfg.get("api_key"):
            os.environ.setdefault(env_key, cfg["api_key"])


def build_provider_state() -> dict[str, Any]:
    """按用户配置建 provider；多模型时包一层 FallbackProvider。"""
    from cli.provider_factory import create_provider, provider_config_kwargs

    state: dict[str, Any] = {"provider": None, "provider_name": "", "model": "", "api_key": "", "base_url": ""}
    try:
        from cli.auth import load_default_model_id, load_model_configs

        configs = load_model_configs()
        default_id = load_default_model_id()
        if configs and default_id:
            seed_model_env(configs)
            default_cfg = next((c for c in configs if c["id"] == default_id), configs[0])
            if len(configs) == 1:
                provider, err = create_provider(**provider_config_kwargs(default_cfg))
                if not err:
                    state.update(default_cfg)
                    state["provider"] = provider
            else:
                from cli.auth import load_fallback_model_id
                from cli.providers.fallback import FallbackProvider

                state.update(default_cfg)
                state["provider"] = FallbackProvider(configs, default_id, fallback_id=load_fallback_model_id())
    except Exception:
        logger.warning("model provider init failed", exc_info=True)
    return state
