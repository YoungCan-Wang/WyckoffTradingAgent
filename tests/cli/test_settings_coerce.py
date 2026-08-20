"""settings_set 的白名单必须每个键都有校验规则。

原来 _coerce_desktop_value 的兜底是 `return value`：任何在 _SETTABLE_KEYS 里
却没写分支的键都被原样写进配置。两个超时键就是这么漏的 —— 存进一个非整数之后，
settings_get 里的 int(...) 抛 ValueError，**整个设置页读不出来**，而用户没法从
界面改回去（读不出来就渲染不了表单），只能手改配置文件。

所以现在兜底是抛错，而不是放过。
"""

from __future__ import annotations

import pytest

from cli.ipc.methods import _SETTABLE_KEYS, MethodError, _coerce_desktop_value

TIMEOUT_KEYS = ("stream_chunk_timeout_seconds", "tool_timeout_seconds")


@pytest.mark.parametrize("key", TIMEOUT_KEYS)
def test_valid_timeout_passes_through(key):
    assert _coerce_desktop_value(key, 120) == 120
    assert _coerce_desktop_value(key, "90") == 90


@pytest.mark.parametrize("key", TIMEOUT_KEYS)
@pytest.mark.parametrize("bad", ["abc", "", None, 3.7, [], {}])
def test_non_integer_timeout_is_rejected(key, bad):
    """这才是原来的杀伤点：坏值存进去之后设置页整页读不出来。"""
    with pytest.raises(MethodError):
        _coerce_desktop_value(key, bad)


@pytest.mark.parametrize("key", TIMEOUT_KEYS)
@pytest.mark.parametrize("out_of_range", [0, -1, 4, 1801, 10**6])
def test_out_of_range_timeout_is_rejected(key, out_of_range):
    """0 等于每次调用立刻超时，超大值等于没有超时 —— 两种都让应用看起来坏掉。"""
    with pytest.raises(MethodError):
        _coerce_desktop_value(key, out_of_range)


def test_theme_is_validated():
    assert _coerce_desktop_value("theme", "dark") == "dark"
    with pytest.raises(MethodError):
        _coerce_desktop_value("theme", "neon")


def test_font_scale_clamps_instead_of_raising():
    """滑块拖到边界属于正常操作，钳制不报错 —— 别在收紧兜底时改掉这个行为。"""
    assert _coerce_desktop_value("desktop_font_scale", 999) == 140
    assert _coerce_desktop_value("desktop_font_scale", 1) == 80


def test_every_settable_key_has_a_rule():
    """穷举：白名单里的每个键都必须被某条分支接住。

    这条是这组测试的重点 —— 它让「往 _SETTABLE_KEYS 加键但忘了写校验」直接变红，
    而不是等用户存进一个坏值锁死设置页才发现。
    """
    samples = {
        "theme": "light",
        "stream_chunk_timeout_seconds": 120,
        "tool_timeout_seconds": 60,
        "desktop_appearance": "light",
        "desktop_font_scale": 100,
        "desktop_font_family": "sans",
        "desktop_density": "cozy",
        "desktop_reduce_motion": False,
        "desktop_send_on_enter": True,
        "desktop_tone": "default",
        "desktop_tone_custom": "",
    }
    unknown = sorted(_SETTABLE_KEYS - samples.keys())
    assert unknown == [], (
        f"这些键在白名单里但这条测试不认识：{unknown}。"
        "给它们补一个合法样例，并确认 _coerce_desktop_value 里有对应分支。"
    )

    for key, value in samples.items():
        if key not in _SETTABLE_KEYS:
            continue
        # 不该抛 invalid_key —— 那说明它掉进了「没有校验规则」的兜底
        try:
            _coerce_desktop_value(key, value)
        except MethodError as exc:
            assert exc.code != "invalid_key", f"{key} 没有对应的校验分支"
