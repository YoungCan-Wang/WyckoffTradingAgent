"""Market-regime trading permission policy."""

from __future__ import annotations

from dataclasses import dataclass

# 硬防守：不送 AI、不写推荐、不新开（与历史 RISK_OFF/CRASH 一致）。
NO_NEW_BUY_REGIMES = frozenset({"UNKNOWN", "RISK_OFF", "CRASH", "BLACK_SWAN"})
# 过热：禁止正式推荐与执行新开，但保留 AI/shadow 对照。
OVERHEAT_SHADOW_REGIMES = frozenset({"RISK_ON"})
REPAIR_REVIEW_REGIMES = frozenset({"BEAR_REBOUND", "PANIC_REPAIR"})
REPAIR_PROBE_REGIMES = frozenset({"PANIC_REPAIR_CONFIRMED", "PANIC_REPAIR_INTRADAY"})
CAUTION_ONLY_REGIMES = frozenset({"CAUTION"})
# 盘前取数失败的专用标记：与 UNKNOWN（盘前模型明确判定「看不清」）区分开，
# 让外部数据源抖动不再冒充风险事件。下游按「缺失」处理，回落到收盘态 benchmark。
PREMARKET_DATA_GAP = "DATA_GAP"
PROBE_ONLY_REGIMES = frozenset(REPAIR_PROBE_REGIMES | CAUTION_ONLY_REGIMES)
# 尾盘/OMS 禁止新开仓的水温并集。
EXECUTE_BLOCK_NEW_BUY_REGIMES = frozenset(NO_NEW_BUY_REGIMES | OVERHEAT_SHADOW_REGIMES | REPAIR_REVIEW_REGIMES)
KNOWN_MARKET_REGIMES = frozenset(
    {
        "RISK_ON",
        "NEUTRAL",
        "CAUTION",
        "BEAR_REBOUND",
        "PANIC_REPAIR",
        "PANIC_REPAIR_CONFIRMED",
        "PANIC_REPAIR_INTRADAY",
        "RISK_OFF",
        "CRASH",
        "BLACK_SWAN",
    }
)
# 数值越小，执行权限越严格；这里按交易权限排序，不按行情涨跌强弱排序。
MARKET_EXECUTION_PRIORITY = {
    "BLACK_SWAN": 0,
    "CRASH_INTRADAY": 1,
    "CRASH": 2,
    "RISK_OFF": 3,
    "UNKNOWN": 4,
    "PANIC_REPAIR": 5,
    "BEAR_REBOUND": 6,
    "RISK_ON": 7,
    "PANIC_REPAIR_INTRADAY": 9,
    "PANIC_REPAIR_CONFIRMED": 9,
    "CAUTION": 10,
    "NEUTRAL": 11,
    "NORMAL": 12,
}


@dataclass(frozen=True)
class MarketTradeMode:
    regime: str
    mode: str
    label: str
    action: str
    reason: str
    allow_ai_review: bool
    allow_recommendation_write: bool
    allow_full_l4: bool
    allow_bypass_review: bool
    allow_theme_promotion: bool


def normalize_regime(regime: str | None) -> str:
    normalized = str(regime or "").strip().upper()
    return normalized if normalized in KNOWN_MARKET_REGIMES else "UNKNOWN"


def stricter_market_regime(first: object, second: object) -> str:
    first_norm = str(first or "").strip().upper()
    second_norm = str(second or "").strip().upper()
    first_norm = first_norm if first_norm in MARKET_EXECUTION_PRIORITY else "UNKNOWN"
    second_norm = second_norm if second_norm in MARKET_EXECUTION_PRIORITY else "UNKNOWN"
    return min((first_norm, second_norm), key=lambda regime: MARKET_EXECUTION_PRIORITY[regime])


def _confirmed_repair_trade_mode(regime: str) -> MarketTradeMode:
    return MarketTradeMode(
        regime=regime,
        mode="repair_probe",
        label="修复成立",
        action="修复成立：只开放一只小额 PROBE，禁止 ATTACK、追价和自动扩仓",
        reason="恐慌后的修复候选已通过次日价格与市场广度双确认",
        allow_ai_review=True,
        allow_recommendation_write=True,
        allow_full_l4=False,
        allow_bypass_review=False,
        allow_theme_promotion=False,
    )


def _explicitly_allowed_regimes() -> frozenset[str]:
    """读 STEP4_BUY_ALLOW_REGIMES —— 运维显式豁免的档位。

    这些档位仍留在 REPAIR_REVIEW / OVERHEAT_SHADOW 等集合里（那些集合同时被 AI 复核、
    推荐写入与横幅文案消费，直接改会牵连数十处），故只在 trade mode 这一层按名单放行。

    为什么需要它：STEP4_BUY_ALLOW_REGIMES 此前只作用于 OMS 的 buy_block_regimes，
    而 max_new_buy_names、build_market_guardrail、本函数三处仍按硬编码集合拦截，
    使豁免形同虚设。#301/#308 又让回测闸门读了同一份 ALLOW，于是形成
    「回测能买、实盘买不到」的错位。

    BEAR_REBOUND 实测（6 天 / 日均 126 只候选 / T+1 开盘买入 → T+5 / 扣 0.202%）：
    净收益 +4.02%、市场 +4.06%、净超额 -0.04pct、为正日恰 50%。即无 alpha 但跟得住
    beta——若替代方案是空仓，放行能吃到那 +4.02%。样本仅 8 天且集中在两周内，
    故这是「可回退的对齐」而非「已证明的优势」：移出 ALLOW 即刻恢复禁买。
    """
    import os

    raw = os.getenv("STEP4_BUY_ALLOW_REGIMES", "").strip()
    return frozenset(item.strip().upper() for item in raw.split(",") if item.strip())


def resolve_market_trade_mode(regime: str | None) -> MarketTradeMode:
    regime_norm = normalize_regime(regime)
    allowed = _explicitly_allowed_regimes()
    if regime_norm in NO_NEW_BUY_REGIMES:
        return MarketTradeMode(
            regime=regime_norm,
            mode="observe_only",
            label="禁止新仓",
            action="禁止新仓：仅影子观察，不送AI、不写推荐、不生成新买入",
            reason=f"{regime_norm} 回测全周期弱势，新开仓胜率不足",
            allow_ai_review=False,
            allow_recommendation_write=False,
            allow_full_l4=False,
            allow_bypass_review=False,
            allow_theme_promotion=False,
        )
    if regime_norm in OVERHEAT_SHADOW_REGIMES and regime_norm not in allowed:
        return MarketTradeMode(
            regime=regime_norm,
            mode="overheat_shadow",
            label="禁止新仓",
            action="禁止新仓：可送AI/shadow 对照，不写正式推荐、不执行新买入",
            reason="RISK_ON 过热追新历史负期望；保留研究样本，禁止正式下单",
            allow_ai_review=True,
            allow_recommendation_write=False,
            allow_full_l4=False,
            allow_bypass_review=False,
            allow_theme_promotion=False,
        )
    if regime_norm in REPAIR_REVIEW_REGIMES and regime_norm not in allowed:
        return MarketTradeMode(
            regime=regime_norm,
            mode="repair_review",
            label="修复观察（禁止新仓）",
            action="仅研究修复候选；不写正式推荐、不生成新仓订单",
            reason=f"{regime_norm} 只适合验证修复强度，禁止自动开仓",
            allow_ai_review=True,
            allow_recommendation_write=False,
            allow_full_l4=False,
            allow_bypass_review=False,
            allow_theme_promotion=False,
        )
    if regime_norm in REPAIR_PROBE_REGIMES:
        return _confirmed_repair_trade_mode(regime_norm)
    if regime_norm in CAUTION_ONLY_REGIMES:
        return MarketTradeMode(
            regime=regime_norm,
            mode="confirmation_only",
            label="谨慎试探",
            action="谨慎试探：最多一只二次确认后的 PROBE，禁止 ATTACK",
            reason="情绪扰动期只开放小额试探，候选必须经过确认支撑",
            allow_ai_review=True,
            allow_recommendation_write=True,
            allow_full_l4=False,
            allow_bypass_review=False,
            allow_theme_promotion=False,
        )
    return MarketTradeMode(
        regime=regime_norm,
        mode="mainline_active",
        label="可执行买入",
        action="可执行买入：主线/趋势买点确认优先，允许主题晋级；关闭噪声旁路",
        reason="中性水温是主战场：主线趋势主导，结构票仅轻量配额",
        allow_ai_review=True,
        allow_recommendation_write=True,
        allow_full_l4=True,
        allow_bypass_review=False,
        allow_theme_promotion=True,
    )
