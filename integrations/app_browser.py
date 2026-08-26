"""驱动桌面应用内置浏览器。

与 ``browser_cdp`` 的区别：那个连的是用户自己的 Chrome（需要手动带
``--remote-debugging-port`` 启动，且会碰到用户真实登录态）；这个走桌面应用
自己托管的隔离视图，端口与 token 由 Electron 在启动时通过环境变量注入。

只在桌面应用内可用。TUI/CLI 下环境变量不存在，调用会明确报「未连接」，
而不是静默退化。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from integrations.browser_cdp import MAX_PAGE_CHARS
from utils.http_security import redact_sensitive_text, validate_public_http_url

ENV_URL = "WYCKOFF_APP_BROWSER_URL"
ENV_TOKEN = "WYCKOFF_APP_BROWSER_TOKEN"

REQUEST_TIMEOUT_SECONDS = 45

# 与 Electron 侧 ACTIONS 白名单对齐。多出来的动作那边会直接拒绝。
ACTIONS = frozenset({"navigate", "text", "title", "url", "click", "fill", "back", "wait"})


def endpoint() -> tuple[str, str] | None:
    url = (os.environ.get(ENV_URL) or "").strip()
    token = (os.environ.get(ENV_TOKEN) or "").strip()
    if not url or not token:
        return None
    return url, token


def is_available() -> bool:
    return endpoint() is not None


def call(action: str, **params: Any) -> dict[str, Any]:
    """向应用控制口发一次动作。返回 dict，失败时带 error 键。"""
    resolved = endpoint()
    if resolved is None:
        return {
            "error": "应用内浏览器不可用：该工具只能在 Wyckoff 桌面应用中使用。",
            "hint": "命令行环境请改用 browser_research（连接本机 Chrome）。",
        }
    if action not in ACTIONS:
        return {"error": f"不支持的动作: {action}（可选: {', '.join(sorted(ACTIONS))}）"}

    # SSRF 防线：Agent 的目标 URL 来自模型，必须挡掉内网地址与非标端口。
    #
    # 这是**两道**独立防线中的一道，不是唯一一道 —— desktop/src/public-url.js
    # 也做完整校验，而且用 session.resolveHost 把解析结果钉死给实际连接
    # （消掉 DNS rebinding 的 TOCTOU）。这里保留同样的检查是因为 CLI/MCP 路径
    # 不经过 Electron。
    # （原注释写「Electron 侧只校验 scheme」，那是早期状态，已不成立。）
    if action == "navigate":
        checked = validate_public_http_url(str(params.get("url") or ""))
        if isinstance(checked, dict):
            return checked
        params = {**params, "url": checked}

    base, token = resolved
    body = json.dumps({"action": action, "params": params}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - 固定回环地址，非用户输入
        base,
        data=body,
        headers={"content-type": "application/json", "x-wyckoff-token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": f"控制口拒绝请求（HTTP {exc.code}）"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"error": f"无法连接应用内浏览器: {exc}"}
    except json.JSONDecodeError:
        return {"error": "控制口返回了非 JSON 响应"}

    if not payload.get("ok"):
        return {"error": str(payload.get("error") or "动作执行失败")}
    return dict(payload.get("result") or {})


def read_page(*, limit: int = MAX_PAGE_CHARS) -> dict[str, Any]:
    """取当前页正文。抄 browser_cdp 的做法做脱敏，页面内容可能含敏感串。"""
    result = call("text")
    if "error" in result:
        return result
    text = str(result.get("text") or "")
    return {
        "url": result.get("url", ""),
        "text": redact_sensitive_text(text[: max(int(limit), 1)]),
    }
