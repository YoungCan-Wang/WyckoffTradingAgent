"""Cursor CLI-style follow-up queue panel for the TUI."""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual.message import Message
from textual.widgets import Static

IDLE_PLACEHOLDER = "问我关于股票的任何问题... (/help 查看命令)"
FOLLOWUP_PLACEHOLDER = "追加跟进…"
FOLLOWUP_HINT = "enter 立即排队 · ↑ 选择/编辑 · esc 取消"
_MAX_PREVIEW = 72
_MAX_VISIBLE = 6
_KIND_LABEL = {
    "system_notification": "后台",
    "schedule": "定时",
}


class FollowUpEditRequested(Message):
    """Input asked to pull the last user follow-up into the composer."""


class FollowUpCancelRequested(Message):
    """Input asked to cancel the current draft or drop the last follow-up."""


class FollowUpsPanel(Static):
    # 边框色与 cli.tui._UI_PALETTES.brand 同步：CSS 无法引用 Python 变量。
    # 运行时再由 apply_brand() 覆盖，避免切主题后仍停在默认琥珀金。
    DEFAULT_CSS = """
    FollowUpsPanel {
        display: none;
        height: auto;
        margin: 0 2;
        padding: 0 1;
        border: solid #e6b450;
        background: $background;
        color: $text;
    }
    Screen.light-mode FollowUpsPanel {
        border: solid #9a6700;
    }
    Screen.transparent FollowUpsPanel {
        background: ansi_default;
        border: solid yellow;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("markup", True)
        super().__init__(*args, **kwargs)

    def apply_brand(self, brand: str) -> None:
        self.styles.border = ("solid", brand)


def preview_followup(content: str, *, limit: int = _MAX_PREVIEW) -> str:
    text = " ".join((content or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def render_followups(
    items: list[Any],
    *,
    selected: int | None = None,
    brand: str = "yellow",
) -> str:
    visible = list(items)
    hidden = 0
    if len(visible) > _MAX_VISIBLE:
        hidden = len(visible) - _MAX_VISIBLE
        visible = visible[-_MAX_VISIBLE:]
        if selected is not None:
            selected = selected - hidden
    lines = [f"[bold {brand}]follow-ups[/bold {brand}]"]
    if hidden:
        lines.append(f"[dim]  …另有 {hidden} 条更早的跟进[/dim]")
    offset = hidden
    for index, item in enumerate(visible):
        kind = getattr(item, "kind", "") or (item.get("kind") if isinstance(item, dict) else "user")
        content = getattr(item, "content", "") or (item.get("content") if isinstance(item, dict) else str(item))
        mark = f"[{brand}]●[/{brand}]" if selected == offset + index else "○"
        label = _KIND_LABEL.get(str(kind), "")
        prefix = f"[dim]{escape(label)}[/dim] " if label else ""
        lines.append(f"  {mark} {prefix}{escape(preview_followup(str(content)))}")
    lines.append(f"[dim]{FOLLOWUP_HINT}[/dim]")
    return "\n".join(lines)
