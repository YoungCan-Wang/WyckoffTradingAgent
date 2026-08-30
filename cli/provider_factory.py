"""Provider 工厂函数 — 从 __main__ 抽出以消除循环导入。"""

from __future__ import annotations

from typing import Any


def provider_config_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_name": config["provider_name"],
        "api_key": config["api_key"],
        "model": config.get("model", ""),
        "base_url": config.get("base_url", ""),
        "context_window": config.get("context_window"),
        "thinking_level": config.get("thinking_level", ""),
    }


def create_provider(
    provider_name: str,
    api_key: str,
    model: str = "",
    base_url: str = "",
    context_window: int | None = None,
    thinking_level: str = "",
):
    import inspect

    from cli.providers import PROVIDERS

    cls = PROVIDERS.get(provider_name)
    if cls is None:
        install_hints = {
            "gemini": "pip install google-genai",
            "claude": "pip install anthropic",
            "openai": "pip install openai",
            "deepseek": "pip install openai",
        }
        hint = install_hints.get(provider_name, "")
        return None, f"Provider '{provider_name}' 不可用，请先安装依赖：{hint}"

    kwargs = {"api_key": api_key}
    if model:
        kwargs["model"] = model
    if base_url:
        kwargs["base_url"] = base_url
    if thinking_level:
        kwargs["thinking_level"] = thinking_level

    sig = inspect.signature(cls.__init__)
    kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

    provider = cls(**kwargs)
    window = _resolved_window(context_window, model, base_url)
    if window > 0:
        provider.context_window = window
    return provider, None


def _resolved_window(context_window: int | None, model: str, base_url: str) -> int:
    """配置值优先；缺失时取 provider 目录报的真实窗口。

    不回退到名字正则：那是 ``resolve_context_window`` 在压缩层的既有行为，
    这里只负责「有真值就用真值」，避免两处各猜一遍且结果不一致。
    """
    try:
        window = int(context_window or 0)
    except (TypeError, ValueError):
        window = 0
    if window > 0:
        return window
    from cli.model_catalog import catalog_context_window, looks_like_openrouter

    if not looks_like_openrouter(base_url):
        return 0
    return int(catalog_context_window(model) or 0)
