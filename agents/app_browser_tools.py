"""Agent 工具：操作桌面应用内置浏览器。

与 ``browser_research`` 分开：那个是「搜一圈拿摘要」的一次性调用，这个是
逐步操作——导航、点击、填表、读正文，适合需要交互才能拿到内容的页面。
"""

from __future__ import annotations

from typing import Any

from integrations.app_browser import call, is_available, read_page


def app_browser(action: str, **kwargs: Any) -> dict[str, Any]:
    """在应用内浏览器里执行一个动作。

    action:
      navigate(url) / read / title / url / click(selector)
      fill(selector, value) / back / wait(ms)
    """
    verb = str(action or "").strip().lower()
    if not verb:
        return {"error": "缺少 action"}

    if not is_available():
        return {
            "error": "应用内浏览器不可用：该工具只能在 Wyckoff 桌面应用中使用。",
            "hint": "命令行环境请改用 browser_research。",
        }

    # read 是本地语义（取正文 + 脱敏），映射到底层的 text 动作。
    if verb == "read":
        limit = kwargs.get("limit")
        return read_page(limit=int(limit)) if limit else read_page()

    if verb == "navigate":
        url = str(kwargs.get("url") or "")
        if not url:
            return {"error": "navigate 需要 url"}
        result = call("navigate", url=url)
        if "error" in result:
            return result
        # 导航后顺手带回正文，省掉一次往返——模型几乎总是接着要读。
        page = read_page()
        return {
            "url": result.get("url", url),
            "title": result.get("title", ""),
            "text": page.get("text", ""),
        }

    if verb in {"click", "fill"}:
        selector = str(kwargs.get("selector") or "")
        if not selector:
            return {"error": f"{verb} 需要 selector"}
        if verb == "click":
            return call("click", selector=selector)
        return call("fill", selector=selector, value=str(kwargs.get("value") or ""))

    if verb == "wait":
        return call("wait", ms=int(kwargs.get("ms") or 500))

    if verb in {"title", "url"}:
        return call(verb)

    return {"error": f"不支持的 action: {verb}"}
