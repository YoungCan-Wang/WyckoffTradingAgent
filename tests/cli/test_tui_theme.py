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


def test_detect_os_dark_mode_mac_bounds_the_subprocess():
    """这是启动路径上的同步调用，必须带 timeout，否则 defaults 卡住会把 TUI 挂在黑屏上。"""
    with patch("sys.platform", "darwin"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "Dark"
            detect_os_dark_mode()

    assert mock_run.call_args.kwargs["timeout"] == 2


def test_detect_os_dark_mode_mac_falls_back_on_timeout():
    import subprocess

    with patch("sys.platform", "darwin"):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("defaults", 2)):
            assert detect_os_dark_mode() is True


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


def test_markdown_code_style_has_no_hardcoded_background():
    """浅色/透明主题下给代码片段硬加纯黑底色，在浅色终端上是一块突兀的黑斑。"""
    for name in ("transparent", "solarized-light", "light", "dracula"):
        app = WyckoffTUI()
        with patch.object(app, "console", MagicMock()) as console:
            app.apply_theme_setting(name)

        theme = console.push_theme.call_args.args[0]
        assert theme.styles["markdown.code"].bgcolor is None, name


def test_markdown_theme_does_not_grow_the_console_theme_stack():
    """push_theme 是压栈而非替换，反复切主题不能让 console 上的主题栈无限变高。"""
    app = WyckoffTUI()
    with patch.object(app, "console", MagicMock()) as console:
        app.apply_theme_setting("dracula")
        app.apply_theme_setting("light")
        app.apply_theme_setting("transparent")

    # 第一次只压不弹，之后每次都先弹掉自己上一次压入的。
    assert console.push_theme.call_count == 3
    assert console.pop_theme.call_count == 2


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
