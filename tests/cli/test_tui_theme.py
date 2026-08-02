"""Unit tests for TUI terminal theme adaptation and /theme slash command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from cli.tui import SUPPORTED_TUI_THEMES, WyckoffTUI, detect_os_dark_mode


def test_supported_themes_dict():
    assert "transparent" in SUPPORTED_TUI_THEMES
    assert "auto" in SUPPORTED_TUI_THEMES
    assert "dracula" in SUPPORTED_TUI_THEMES
    assert "nord" in SUPPORTED_TUI_THEMES
    assert "monokai" in SUPPORTED_TUI_THEMES
    assert "solarized-dark" in SUPPORTED_TUI_THEMES
    assert "solarized-light" in SUPPORTED_TUI_THEMES


def test_detect_os_dark_mode_mac():
    with patch("sys.platform", "darwin"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "Dark"
            assert detect_os_dark_mode() is True

            mock_run.return_value.stdout = ""
            assert detect_os_dark_mode() is False


def test_detect_os_dark_mode_win():
    with patch("sys.platform", "win32"):
        mock_winreg = MagicMock()
        mock_winreg.OpenKey.return_value = MagicMock()
        mock_winreg.QueryValueEx.return_value = (0, 1)  # 0 means Dark mode
        with patch.dict("sys.modules", {"winreg": mock_winreg}):
            assert detect_os_dark_mode() is True


def test_apply_theme_setting_transparent():
    app = WyckoffTUI()
    res = app.apply_theme_setting("transparent")
    assert res == "transparent"
    assert app.theme == "textual-dark"


def test_apply_theme_setting_dracula():
    app = WyckoffTUI()
    res = app.apply_theme_setting("dracula")
    assert res == "dracula"
    assert app.theme == "dracula"


def test_apply_theme_setting_auto():
    app = WyckoffTUI()

    with patch("cli.tui.detect_os_dark_mode", return_value=True):
        res = app.apply_theme_setting("auto")
        assert res == "transparent"

    with patch("cli.tui.detect_os_dark_mode", return_value=False):
        res = app.apply_theme_setting("auto")
        assert res == "solarized-light"


def test_handle_theme_cmd_list():
    app = WyckoffTUI()
    log = MagicMock()

    app._handle_theme_cmd("/theme list", log)
    log.write.assert_called_once()
    output_text = log.write.call_args[0][0].plain
    assert "可用终端主题" in output_text
    assert "dracula" in output_text
    assert "transparent" in output_text


def test_handle_theme_cmd_set():
    app = WyckoffTUI()
    log = MagicMock()

    with patch("cli.auth.save_config_key") as mock_save:
        app._handle_theme_cmd("/theme nord", log)
        assert app.theme == "nord"
        mock_save.assert_called_with("theme", "nord")
        log.write.assert_called_once()
        assert "✓ 已设置主题为: nord" in log.write.call_args[0][0].plain
