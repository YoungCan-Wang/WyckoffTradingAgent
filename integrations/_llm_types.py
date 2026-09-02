"""Shared LLM provider constants without importing provider SDKs."""

from __future__ import annotations

from urllib.parse import urlparse

SUPPORTED_PROVIDERS = (
    "1route",
    "gemini",
    "openai",
    "longcat",
    "efficiency",
    "openai_compatible",
    "zhipu",
    "minimax",
    "deepseek",
    "qwen",
    "volcengine",
)

OPENAI_COMPATIBLE_BASE_URLS = {
    "1route": "https://api.1route.dev/v1",
    "openai": "https://api.openai.com/v1",
    "longcat": "https://api.longcat.chat/openai",
    "efficiency": "",
    "openai_compatible": "",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "minimax": "https://api.minimaxi.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "volcengine": "https://ark.cn-beijing.volces.com/api/v3",
}

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite-preview"
GEMINI_MODELS = (
    DEFAULT_GEMINI_MODEL,
    "gemini-2.5-flash-lite",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
)


def normalize_openai_compatible_base_url(base_url: str) -> str:
    """1Route 根域名会 200 回官网 HTML，必须落到 /v1。"""
    text = (base_url or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    if host == "api.1route.dev" and path in {"", "/"}:
        return f"{parsed.scheme}://{parsed.netloc}/v1"
    return text


PROVIDER_LABELS: dict[str, str] = {
    "1route": "1Route（推荐）",
    "gemini": "Gemini",
    "openai": "OpenAI",
    "longcat": "LongCat",
    "efficiency": "Efficiency（OpenAI兼容）",
    "openai_compatible": "OpenAI兼容",
    "zhipu": "智谱",
    "minimax": "Minimax",
    "deepseek": "DeepSeek",
    "qwen": "Qwen",
    "volcengine": "火山引擎",
}
