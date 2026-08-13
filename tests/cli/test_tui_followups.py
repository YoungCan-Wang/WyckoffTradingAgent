from __future__ import annotations

from cli.conversation.session import ConversationSession, QueuedInput
from cli.tui import _ui_palette
from cli.tui_followups import FOLLOWUP_HINT, FollowUpsPanel, preview_followup, render_followups


def test_preview_followup_collapses_whitespace_and_truncates():
    assert preview_followup("  hello\nworld  ") == "hello world"
    assert preview_followup("x" * 80).endswith("…")
    assert len(preview_followup("x" * 80)) == 72


def test_render_followups_matches_cursor_cli_shape():
    markup = render_followups(
        [
            QueuedInput(kind="user", content="减仓华勤"),
            QueuedInput(kind="schedule", content="盘前风控"),
        ]
    )
    assert "[bold yellow]follow-ups[/bold yellow]" in markup
    assert "○" in markup
    assert "定时" in markup
    assert FOLLOWUP_HINT in markup
    assert "减仓华勤" in markup


def test_render_followups_hides_older_items():
    items = [QueuedInput(kind="user", content=f"q{i}") for i in range(8)]
    markup = render_followups(items)
    assert "另有 2 条更早的跟进" in markup
    assert "q0" not in markup
    assert "q7" in markup


def test_render_followups_uses_theme_brand():
    cases = {
        "transparent": "yellow",
        "terminal": "yellow",
        "dracula": "#e6b450",
        "nord": "#e6b450",
        "solarized-light": "#9a6700",
        "light": "#9a6700",
    }
    item = QueuedInput(kind="user", content="hi")
    for theme, brand in cases.items():
        assert _ui_palette(theme)["brand"] == brand
        markup = render_followups([item], brand=brand)
        assert f"[bold {brand}]follow-ups[/bold {brand}]" in markup


def test_followups_panel_css_covers_palette_families():
    css = FollowUpsPanel.DEFAULT_CSS
    assert "border: solid yellow;" in css
    assert "border: solid #e6b450;" in css
    assert "border: solid #9a6700;" in css
    assert "Screen.transparent FollowUpsPanel" in css
    assert "Screen.light-mode FollowUpsPanel" in css


def test_tui_css_transparent_uses_ansi_yellow_brand():
    from cli.tui import WyckoffTUI

    css = WyckoffTUI.CSS
    assert "Screen.transparent #input-container:focus-within" in css
    assert "border: round yellow;" in css
    assert "Screen.transparent #prompt-prefix" in css


def test_pop_last_user_followup_skips_system_items():
    session = ConversationSession()
    session.enqueue("first")
    session.enqueue(QueuedInput(kind="system_notification", content="bg"))
    session.enqueue("second")
    taken = session.pop_last_user_followup()
    assert taken is not None
    assert taken.content == "second"
    remaining = [item.content for item in session.input_queue]
    assert remaining == ["first", "bg"]
    assert session.pop_last_user_followup().content == "first"
    assert session.pop_last_user_followup() is None
    assert [item.kind for item in session.input_queue] == ["system_notification"]
