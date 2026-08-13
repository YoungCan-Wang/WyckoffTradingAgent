"""Agent 工具：把分析结论画到 K 线图上。

和 ``wyckoff_diagnose`` 分工不同：那个返回文字结论，这个把结论落成图上的
形状（吸筹区矩形、支撑阻力线、spring 标记）。图表打开时读取，所以先画后开、
先开后画都成立。

标注是纯展示 —— 不动持仓、不下单、不花钱，因此归读工具，不进审批队列。
"""

from __future__ import annotations

from typing import Any

from integrations.chart_annotations import (
    MAX_PER_CHART,
    REQUIRED,
    AnnotationError,
    clear,
    load,
    make_chart_id,
    replace,
)


def annotate_chart(
    code: str,
    annotations: list[Any] | None = None,
    action: str = "draw",
    timeframe: str = "1d",
) -> dict[str, Any]:
    """在某只股票的 K 线图上画标注。

    action:
      draw  —— 用 annotations 整组替换该图现有标注（重画即编辑）
      list  —— 列出该图当前标注
      clear —— 清空该图标注

    annotations 是一个数组，每条按 type 判别：
      rectangle {type, start_date, end_date, low, high, label?}
        —— 吸筹区/派发区/震荡箱
      price_line {type, price, label?}        —— 支撑/阻力/目标位
      trendline {type, start_date, start_price, end_date, end_price, label?}
        —— 供需线/通道边
      marker {type, date, price, label?}      —— spring/upthrust 锚在单根 K 线
      text   {type, date, price, text}        —— 事件字母 PS/SC/AR/ST/LPS/SOS

    日期用 YYYY-MM-DD，价格用数字。
    """
    symbol = str(code or "").strip()
    if not symbol:
        return {"error": "缺少 code"}
    chart_id = make_chart_id(symbol, timeframe)
    verb = str(action or "draw").strip().lower()

    if verb == "list":
        items = load(chart_id)
        return {"chart_id": chart_id, "annotations": items, "count": len(items)}

    if verb == "clear":
        return {"chart_id": chart_id, "cleared": clear(chart_id)}

    if verb != "draw":
        return {"error": f"不支持的 action: {verb}；可用 draw / list / clear"}

    items = annotations or []
    if not isinstance(items, list):
        return {"error": "annotations 必须是数组"}
    if not items:
        return {"error": "draw 需要至少一条标注；要清空请用 action=clear"}

    try:
        # fail-closed：先落盘成功才回报成功。反过来会让图上出现"幽灵画线" ——
        # 界面有、重开就没。
        saved = replace(chart_id, items)
    except AnnotationError as exc:
        return {
            "error": str(exc),
            "hint": f"每种 type 的必填字段：{ {k: list(v) for k, v in REQUIRED.items()} }",
        }
    except OSError as exc:
        return {"error": f"标注写入失败：{exc.strerror or '磁盘错误'}"}

    return {
        "chart_id": chart_id,
        "drawn": len(saved),
        "limit": MAX_PER_CHART,
        "note": "已保存；K 线图打开或刷新时显示",
    }
