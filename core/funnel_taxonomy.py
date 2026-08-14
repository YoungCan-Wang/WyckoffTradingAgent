"""User-facing funnel taxonomy.

Internal Wyckoff layers remain useful metrics, but product surfaces should
describe the current funnel as base admission -> candidate lanes -> confirmation.
"""

from __future__ import annotations

REVIEW_STAGE_CANDIDATE_HIT = "候选池已捕获"
REVIEW_STAGE_BASE_REJECT = "基础准入淘汰"
REVIEW_STAGE_STRENGTH_MISS = "未入候选池[结构强度不足]"
REVIEW_STAGE_THEME_MISS = "未入候选池[题材共振不足]"
REVIEW_STAGE_RISK_BLOCK = "风控拦截"
REVIEW_STAGE_TRIGGER_HIT = "买点已确认"
REVIEW_STAGE_TRIGGER_MISS = "买点未确认"

SOURCE_LABELS = {
    "mainline": "主线买点",
    "alpha_candidate": "候选车道",
    "l4_hit": "L4量价触发",
    "l4_springboard": "L4量价触发",
    "l2_bypass": "形态旁路观察",
    "strategic_l2_bypass": "战略主题观察",
    "l3_fill": "板块阶段补位",
    "markup": "主升阶段补位",
    "accum_c": "吸筹确认补位",
    "signal_confirmed": "跨日确认",
    "dynamic_shadow_promotion": "动态影子晋级",
}

LANE_LABELS = {
    "mainline": "主线买点",
    "trend_breakout": "趋势突破",
    "trend_lane_pullback": "趋势回踩",
    "sector_strength": "板块强势",
    "wyckoff_structure": "Wyckoff结构",
    "sos": "SOS点火",
    "evr": "EVR放量不跌",
    "lps": "LPS缩量回踩",
    "spring": "Spring震仓",
    "early_breakout": "早期突破",
    "volatile_pullback": "波动回踩",
    "launchpad": "启动平台",
}


def source_label(source: str) -> str:
    clean = str(source or "").strip()
    return SOURCE_LABELS.get(clean, clean)


def lane_label(lane: str) -> str:
    clean = str(lane or "").strip()
    return LANE_LABELS.get(clean, clean)
