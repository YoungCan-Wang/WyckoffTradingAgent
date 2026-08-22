"""可交互产物：模型生成的 HTML 面板，在桌面端隔离视图里渲染。

## 为什么这个工具存在

K 线图能表达的东西是固定的（蜡烛 + 几种标注）。但有些结论需要别的形状：
持仓的行业分布、多因子对比表、可筛选的候选列表。与其为每一种做一个专用面板，
不如让模型直接产出 HTML —— 它本来就擅长这个。

## 为什么允许执行 JS 是安全的

渲染它的视图（desktop/src/artifact-host.js）阻断了**一切**网络：独立 session
分区、onBeforeRequest 取消所有非 data: 请求、无 preload、CSP `default-src 'none'`。
所以模型生成的代码能做动画和交互，但拿不到宿主 API，也没有任何出网通道 ——
允许执行之后真正的风险是「把持仓数据 fetch 出去」，而那条路是断的。

这也意味着：**产物拿不到实时数据**。要展示什么，就得在生成时把数据嵌进 HTML。
这是刻意的取舍 —— 一个能自己取数的产物就是一个能自己外泄的产物。

## 这个工具不做校验

不解析 HTML、不过滤标签、不检查 script 内容。理由：任何「先解析再放行」的
白名单都会漏（HTML 解析歧义是攻击面本身），而隔离层已经让「漏了」不产生后果。
用解析器当安全边界是常见的错误设计。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# HTML 体积上限。模型偶尔会把一整个库内联进来，那样事件会很大而且渲染很慢。
# 512 KiB 足够放一个自绘的图表 + 内嵌数据，又不至于把 IPC 通道堵住。
MAX_HTML_BYTES = 512 * 1024

# 文件名允许的字符。与 report_artifact_tools 一致 —— 报告库是给人看的，中文保留。
_UNSAFE_NAME = re.compile(r"[^0-9A-Za-z一-鿿 _-]+")


def _user_id(tool_context: Any) -> str:
    """同 report_artifact_tools：产物按账号隔离，拿不到身份落 __anon__。"""
    return str(getattr(tool_context, "user_id", "") or "")


def _slug(title: str) -> str:
    cleaned = _UNSAFE_NAME.sub("", title).strip().strip(".")
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:60] or "dashboard"


def render_dashboard(
    title: str,
    html: str,
    tool_context: Any = None,
) -> dict[str, Any]:
    """在桌面端渲染一个可交互的 HTML 面板。

    只在 Wyckoff 桌面应用里可用。返回值只带元信息，HTML 本身通过产物事件
    送到前端 —— 不放进工具结果，因为工具结果会进模型上下文，几百 KB 的
    HTML 回灌一遍纯属浪费。
    """
    name = str(title or "").strip()
    if not name:
        return {"error": "需要 title"}

    body = str(html or "")
    if not body.strip():
        return {"error": "需要 html 内容"}

    size = len(body.encode("utf-8"))
    if size > MAX_HTML_BYTES:
        return {
            "error": (
                f"HTML 太大（{size // 1024} KiB，上限 {MAX_HTML_BYTES // 1024} KiB）。"
                "不要内联整个图表库，用原生 canvas/svg 自绘，或减少内嵌数据。"
            )
        }

    # 落盘，让面板在刷新窗口、重启应用之后还能找回来。
    #
    # 文件名用 .dash.html 而不是 .html：报告库把 `.html` 当**静态文档**渲染
    # （`sandbox=""`，不给 allow-scripts）。一个可交互面板用那条路径打开会变成
    # 一张死页面 —— 按钮点不动、图表不画，而且**没有任何提示**。
    # 静默降级比不持久化更糟，所以用自己的后缀，由桌面端走隔离视图重开。
    rel = ""
    try:
        from integrations.report_store import ensure_reports_dir, resolve_inside_reports

        uid = _user_id(tool_context)
        ensure_reports_dir(uid)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = _slug(name)
        rel = f"{stamp}-{slug}.dash.html"
        target = resolve_inside_reports(rel, uid)
        seq = 2
        while target.exists():
            rel = f"{stamp}-{slug}-{seq}.dash.html"
            target = resolve_inside_reports(rel, uid)
            seq += 1
        target.write_text(body, encoding="utf-8")
    except Exception:
        # 落盘失败不该让面板打不开 —— 那是「能用但关掉就没了」vs「压根没有」。
        logger.warning("dashboard persist failed", exc_info=True)
        rel = ""

    # 返回值刻意不含 html：工具结果会进模型上下文，把刚生成的几百 KB 回灌一遍
    # 既浪费 token 又可能挤掉真正的对话历史。前端从产物事件拿 HTML。
    return {
        "rendered": True,
        "title": name,
        "bytes": size,
        "path": rel,
        "note": "面板已在桌面端右侧打开。它是隔离渲染的，拿不到实时数据，也不能联网。",
    }
