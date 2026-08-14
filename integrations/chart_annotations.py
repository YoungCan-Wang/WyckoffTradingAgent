"""图表标注存储 — agent 画在 K 线图上的东西存在这里。

设计取自 LangAlpha 的 chart_annotation：

* ``chart_id = "SYMBOL:timeframe"`` 作为图表身份，同一支同周期重画即编辑，
  换标的或换周期自然是另一张图。
* **判别式联合**：每种标注一组自己的必填字段，靠 ``type`` 判别。模型看到的是
  干净的"这几种形状各要什么"，而不是一堆可空字段。
* **fail-closed**：先落盘成功才回报成功。反过来做会让界面上出现"幽灵画线" ——
  图上有、重开就没了。

字段长度和数量都封顶：标注文本会进模型上下文也会进界面，跑飞的 agent
不该能把存储和上下文一起撑爆。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

STORE_PATH = Path.home() / ".wyckoff" / "annotations.json"

# 只有桌面端的渲染层能把标注画出来。CLI/TUI/MCP 下写入会成功但没人看得见，
# 模型却会把它当成「已经画好了」汇报给用户 —— 那是在报告一件没发生的事。
# 由 cli.ipc.stdio.serve() 在启动时打开，因为 `cli ipc` 是唯一带渲染端的入口。
_renderer_available = False


def set_renderer_available(available: bool) -> None:
    """声明当前进程有能显示标注的界面。"""
    global _renderer_available
    _renderer_available = bool(available)


def renderer_available() -> bool:
    return _renderer_available


MAX_PER_CHART = 60
MAX_TEXT = 60
MAX_CHARTS = 200

# 每种标注的必填字段。威科夫用得上的先做这几种；矩形是主力（吸筹/派发区）。
REQUIRED: dict[str, tuple[str, ...]] = {
    "rectangle": ("start_date", "end_date", "low", "high"),
    "price_line": ("price",),
    "trendline": ("start_date", "start_price", "end_date", "end_price"),
    "marker": ("date", "price"),
    "text": ("date", "price", "text"),
}

# 允许出现的可选字段，其余一律丢弃 —— 不让未知键进存储。
OPTIONAL = ("label", "color", "note")

_DATE_FIELDS = ("date", "start_date", "end_date")
_NUM_FIELDS = ("price", "low", "high", "start_price", "end_price")


class AnnotationError(ValueError):
    """标注不合法。消息面向模型，需要说清楚哪里不对。"""


def make_chart_id(symbol: str, timeframe: str = "1d") -> str:
    return f"{str(symbol).strip().upper()}:{str(timeframe).strip() or '1d'}"


def validate(item: Any) -> dict[str, Any]:
    """校验并规范化一条标注，返回只含已知字段的干净 dict。"""
    if not isinstance(item, dict):
        raise AnnotationError("每条标注必须是对象")
    kind = str(item.get("type") or "").strip()
    if kind not in REQUIRED:
        raise AnnotationError(f"不支持的 type: {kind or '(空)'}；可用：{', '.join(sorted(REQUIRED))}")

    out: dict[str, Any] = {"type": kind}
    for field in REQUIRED[kind]:
        if field not in item or item[field] is None:
            raise AnnotationError(f"{kind} 缺少必填字段 {field}")
        out[field] = _clean_field(field, item[field])
    for field in OPTIONAL:
        if item.get(field) is not None:
            out[field] = _clean_text(item[field])

    # 矩形上下颠倒是常见笔误，直接扶正而不是报错。
    if kind == "rectangle" and out["low"] > out["high"]:
        out["low"], out["high"] = out["high"], out["low"]
    return out


def _clean_field(field: str, value: Any) -> Any:
    if field in _NUM_FIELDS:
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise AnnotationError(f"{field} 必须是数字") from exc
        # NaN/Inf 进了 JSON 会让前端 JSON.parse 拿到非法值，也画不出来。
        if math.isnan(num) or math.isinf(num):
            raise AnnotationError(f"{field} 不能是 NaN 或 Inf")
        return num
    if field in _DATE_FIELDS:
        text = str(value).strip()[:10]
        if len(text) != 10 or text[4] != "-" or text[7] != "-":
            raise AnnotationError(f"{field} 需要 YYYY-MM-DD 格式，收到 {value!r}")
        return text
    return _clean_text(value)


def _clean_text(value: Any) -> str:
    return str(value).replace("\n", " ").strip()[:MAX_TEXT]


def load_all(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    target = path or STORE_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, list)}


def load(chart_id: str, path: Path | None = None) -> list[dict[str, Any]]:
    return load_all(path).get(chart_id, [])


def replace(chart_id: str, items: list[Any], path: Path | None = None) -> list[dict[str, Any]]:
    """整组替换某张图的标注 —— 重画即编辑，不做增量合并。

    先全部校验再落盘：一条不合法就整批拒绝，避免图上出现半套标注。
    """
    if len(items) > MAX_PER_CHART:
        raise AnnotationError(f"一张图最多 {MAX_PER_CHART} 条标注，收到 {len(items)}")
    cleaned = [validate(item) for item in items]

    target = path or STORE_PATH
    store = load_all(target)
    if cleaned:
        store[chart_id] = cleaned
    else:
        store.pop(chart_id, None)
    # 图太多就丢最早的键，存储不该无限长。
    if len(store) > MAX_CHARTS:
        for key in list(store)[: len(store) - MAX_CHARTS]:
            store.pop(key, None)
    _write(target, store)
    return cleaned


def clear(chart_id: str, path: Path | None = None) -> int:
    target = path or STORE_PATH
    store = load_all(target)
    removed = len(store.pop(chart_id, []))
    _write(target, store)
    return removed


def _write(target: Path, store: dict[str, Any]) -> None:
    """原子写：直接覆盖时进程被杀会留下截断的 JSON，下次全丢。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".annotations-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(store, fh, ensure_ascii=False)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
