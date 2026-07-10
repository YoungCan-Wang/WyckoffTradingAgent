"""Backtest helper import contract tests."""

from __future__ import annotations

import pytest

akshare = pytest.importorskip("akshare", reason="akshare not installed")


def test_backtest_helpers_are_importable_from_owner_modules():
    """确认回测 helper 从真实归属模块可正常 import。"""
    from core.backtest_metrics import calc_max_drawdown_pct, fmt_metric
    from core.backtest_run import parse_date

    assert callable(calc_max_drawdown_pct)
    assert callable(parse_date)
    assert callable(fmt_metric)
