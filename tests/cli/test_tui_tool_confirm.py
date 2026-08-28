from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult
from textual.widgets import Static

from cli.tui import ToolConfirmScreen


class _ConfirmHarness(App):
    def compose(self) -> ComposeResult:
        yield Static("background")


def test_long_command_keeps_confirmation_options_visible() -> None:
    asyncio.run(_assert_long_command_layout())


async def _assert_long_command_layout() -> None:
    command = 'python3 -c "\n' + "\n".join(f"print({line!r})" for line in range(24)) + '\n"'
    app = _ConfirmHarness()

    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ToolConfirmScreen("exec_command", {"command": command}, "执行命令"))
        await pilot.pause()

        box = app.screen.query_one("#confirm-box")
        summary_scroll = app.screen.query_one("#confirm-summary-scroll")
        options = app.screen.query_one("#confirm-options")

        assert summary_scroll.size.height <= 8
        assert summary_scroll.virtual_size.height > summary_scroll.size.height
        assert options.size.height == 4
        assert options.region.bottom <= box.region.bottom
