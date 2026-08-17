"""把滑点配置从环境变量注入 core.trade_friction。

core 层不允许直接读环境变量（tests/test_architecture_boundaries.py 会拦），
所以环境读取放在 utils 侧，由脚本／作业入口调用一次。
"""

from __future__ import annotations

import os

from core.trade_friction import DEFAULT_SLIPPAGE_BPS_PER_SIDE, configure_slippage

ENV_KEY = "WYCKOFF_SLIPPAGE_BPS_PER_SIDE"


def apply_friction_config_from_env() -> float:
    """读取 env 并注入 core，返回生效的单边滑点（bps）。"""
    raw = os.getenv(ENV_KEY, "").strip()
    if not raw:
        configure_slippage(DEFAULT_SLIPPAGE_BPS_PER_SIDE)
        return DEFAULT_SLIPPAGE_BPS_PER_SIDE
    try:
        value = max(0.0, float(raw))
    except ValueError:
        value = DEFAULT_SLIPPAGE_BPS_PER_SIDE
    configure_slippage(value)
    return value
