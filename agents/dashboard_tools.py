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
from typing import Any

logger = logging.getLogger(__name__)

# HTML 体积上限。模型偶尔会把一整个库内联进来，那样事件会很大而且渲染很慢。
# 512 KiB 足够放一个自绘的图表 + 内嵌数据，又不至于把 IPC 通道堵住。
MAX_HTML_BYTES = 512 * 1024


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

    # 返回值刻意不含 html：工具结果会进模型上下文，把刚生成的几百 KB 回灌一遍
    # 既浪费 token 又可能挤掉真正的对话历史。前端从产物事件拿 HTML。
    return {
        "rendered": True,
        "title": name,
        "bytes": size,
        "note": "面板已在桌面端右侧打开。它是隔离渲染的，拿不到实时数据，也不能联网。",
    }
