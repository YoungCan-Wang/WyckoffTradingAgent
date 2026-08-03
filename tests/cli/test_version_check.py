"""Unit tests for the CLI update-notice version comparison."""

from __future__ import annotations

import pytest

from cli.__main__ import _version_sort_key


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.9.234", (0, 9, 234)),
        ("0.10.0", (0, 10, 0)),
        ("1.0.0", (1, 0, 0)),
        # 非纯数字后缀原先会让 int() 抛 ValueError，升级提示随之静默失效
        ("1.0.0rc1", (1, 0, 0)),
        ("0.9.235.post1", (0, 9, 235)),
        ("  0.9.234  ", (0, 9, 234)),
    ],
)
def test_version_sort_key_parses_pypi_shapes(value: str, expected: tuple[int, ...]) -> None:
    assert _version_sort_key(value) == expected


@pytest.mark.parametrize("value", ["", "garbage", "vNext", "."])
def test_version_sort_key_returns_empty_for_unparseable(value: str) -> None:
    """无法解析时返回空元组，让调用方跳过比较而不是拿残缺版本号误报。"""
    assert _version_sort_key(value) == ()


def test_version_sort_key_orders_prerelease_above_current_patch() -> None:
    """1.0.0rc1 必须被认成比 0.9.234 新——大版本恰恰是最需要提示的时候。"""
    assert _version_sort_key("1.0.0rc1") > _version_sort_key("0.9.234")


def test_version_sort_key_does_not_flag_older_release() -> None:
    assert not _version_sort_key("0.9.200") > _version_sort_key("0.9.234")
