"""Runtime configuration loader for Step4 OMS order rules."""

from __future__ import annotations

import os

from core.market_trade_mode import EXECUTE_BLOCK_NEW_BUY_REGIMES, buy_allow_regimes_from_env
from utils.env import env_bool as _env_bool
from utils.env import env_float as _env_float
from utils.env import env_int as _env_int
from workflows.step4_models import Step4OrderConfig


def step4_order_config_from_env() -> Step4OrderConfig:
    gap_min = max(_env_float("STEP4_CHASE_GAP_PCT_MIN", 1.2), 0.2)
    atr_min = max(_env_float("STEP4_CHASE_ATR_MULT_MIN", 0.8), 0.1)
    return Step4OrderConfig(
        atr_multiplier=_env_float("STEP4_ATR_MULTIPLIER", 2.0),
        buy_hard_stop_enabled=_env_bool("STEP4_BUY_HARD_STOP_ENABLED", True),
        buy_hard_stop_pct=max(_env_float("STEP4_BUY_HARD_STOP_PCT", 12.0), 0.0),
        buy_stop_mode=_env_stop_mode("STEP4_BUY_STOP_MODE", "floor"),
        atr_slippage_factor=_env_float("STEP4_ATR_SLIPPAGE_FACTOR", 0.25),
        probe_budget_limit=_clamp01(_env_float("STEP4_PROBE_BUDGET_LIMIT", 0.10)),
        repair_probe_budget_limit=_clamp01(_env_float("STEP4_REPAIR_PROBE_BUDGET_LIMIT", 0.05)),
        attack_budget_limit=_clamp01(_env_float("STEP4_ATTACK_BUDGET_LIMIT", 0.20)),
        buy_block_regimes=_env_regime_set(
            "STEP4_BUY_BLOCK_REGIMES",
            "RISK_ON,BEAR_REBOUND,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN",
        ),
        block_buy_on_stale_exit=_env_bool("STEP4_BLOCK_BUY_ON_STALE_EXIT", True),
        new_position_stop_guard_days=_env_int("STEP4_NEW_POSITION_STOP_GUARD_DAYS", 4, minimum=0),
        chase_gap_pct_min=gap_min,
        chase_gap_pct_max=max(_env_float("STEP4_CHASE_GAP_PCT_MAX", 5.5), gap_min),
        chase_atr_mult_min=atr_min,
        chase_atr_mult_max=max(_env_float("STEP4_CHASE_ATR_MULT_MAX", 2.4), atr_min),
        max_gap_up_pct=_env_float("STEP4_MAX_GAP_UP_PCT", 3.0),
        max_gap_up_atr_mult=_env_float("STEP4_MAX_GAP_UP_ATR_MULT", 1.5),
    )


def _env_stop_mode(name: str, default: str) -> str:
    mode = os.getenv(name, default).strip().lower()
    return mode if mode in {"fixed", "floor"} else default


def _env_regime_set(name: str, default: str) -> frozenset[str]:
    """解析禁买水温集合。

    ``EXECUTE_BLOCK_NEW_BUY_REGIMES`` 无条件并入，所以单改 env 无法放开其中任何一档；
    但那个集合同时被 AI 复核、推荐写入与横幅文案消费（实测直接改它会连带影响 30 个用例），
    因此放开走 ``STEP4_BUY_ALLOW_REGIMES`` 显式豁免，并须贯穿：OMS ``buy_block_regimes``、
    ``max_new_buy_names``、guardrail 文案、以及 ``resolve_market_trade_mode`` 的推荐写入开关。

    2026-08-17 联动实测（水温 × 严格候选，54 个交易日，候选 T+5 相对同日全市场）：

    | 水温 | 天数 | 超额 | 为正 |
    |---|---:|---:|---:|
    | RISK_ON | 3 | +6.07pct | 3/3 |
    | BEAR_REBOUND | 4 | +4.08pct | 4/4 |
    | NEUTRAL | 12 | −4.35pct | 3/12 |

    现状放行档（NEUTRAL/CAUTION）13 天超额 −4.62pct、bootstrap 95% CI [−6.99, −2.33]；
    换成 BEAR_REBOUND/RISK_ON 后 7 天超额 +4.93pct、CI [+3.18, +6.65]，两个区间不重叠。
    RISK_ON/BEAR_REBOUND 样本仅 3~4 天，故做成**可关的显式开关**而非改默认策略常量，
    并保留 NEUTRAL 于禁买（它证据最强：CI [−6.80, −2.07] 不跨 0，且大盘侧独立同向 p=0.011）。
    """
    values = {
        item.strip().upper()
        for item in os.getenv(name, default).split(",")
        if item.strip() and item.strip().upper() != "COOLDOWN"
    }
    merged = values | set(EXECUTE_BLOCK_NEW_BUY_REGIMES)
    return frozenset(merged - buy_allow_regimes_from_env())


def _clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)
