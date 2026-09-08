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
# 运维可以显式降到 PROBE 档（而非完全禁买）的硬防守水温白名单。
# 只有 RISK_OFF：CRASH/BLACK_SWAN/UNKNOWN 不在其中，见 _probe_downgraded_regimes 的实测依据。
PROBE_DOWNGRADABLE_REGIMES = frozenset({"RISK_OFF"})
# 盘前取数失败的专用标记：与 UNKNOWN（盘前模型明确判定「看不清」）区分开，
# 让外部数据源抖动不再冒充风险事件。下游按「缺失」处理，回落到收盘态 benchmark。
PREMARKET_DATA_GAP = "DATA_GAP"
# 盘前态（A50/VIX）唯一还能收紧执行权限的档位。依据见 merge_premarket_regime。
PREMARKET_ESCALATION_REGIMES = frozenset({"BLACK_SWAN"})
PROBE_ONLY_REGIMES = frozenset(REPAIR_PROBE_REGIMES | CAUTION_ONLY_REGIMES)
# 尾盘/OMS 禁止新开仓的水温并集。
EXECUTE_BLOCK_NEW_BUY_REGIMES = frozenset(NO_NEW_BUY_REGIMES | OVERHEAT_SHADOW_REGIMES | REPAIR_REVIEW_REGIMES)
# STEP4_BUY_BLOCK_REGIMES 的默认值。注意默认不含 NEUTRAL，生产 env 才加——
# 见 oms_buy_block_regimes() 对两侧闸门错位的说明。
_DEFAULT_OMS_BUY_BLOCK = "RISK_ON,BEAR_REBOUND,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN"
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


def merge_premarket_regime(benchmark: object, premarket: object) -> str:
    """收盘态叠加盘前态——**唯一**的合并口径，执行层与横幅文案都必须走它。

    以 benchmark（境内价量宽度）为准，盘前态（A50/VIX）只保留 BLACK_SWAN 一条收紧
    通道；``RISK_OFF``/``UNKNOWN``/``CAUTION``/缺失/``DATA_GAP``/拼写错误一律回落
    benchmark 的判定。

    为什么裁掉那两条降级路径——69 个交易日实测（05-25 ~ 08-28）：

    - 9 天改了标签，其中 6 天真的改了 ``allow_ai_review``、3 天改了
      ``allow_recommendation_write``。
    - 那 6 天的候选前瞻超额（漏斗当日真实候选 / T+1 开盘买 → T+5 / 扣 0.202% /
      基准为同日同流动性门槛的全市场等权）：05-26 -1.30、06-18 **+2.93**、
      06-24 -1.38、07-14 **-7.32**、08-07 **+0.91**、08-17 **+0.80**。
      3 次挡对 / 3 次挡错，均值 -0.89 全由 07-14 一天贡献，剔掉它翻正为 +0.39，
      中位数 +0.80。
    - 3 天 UNKNOWN **全部**是取数失败而非「看不清」：05-26 是 VIX
      ``timeout_fallback``，08-07/08-17 是 ``a50_pct_chg`` 缺失。08-17 一天就白扔
      71 只 formal_l4 / 168 只候选。
    - 3 天 RISK_OFF 全部由 ``VIX涨幅 >= 8%`` 单条触发，而 VIX 取的是美股前一交易日
      收盘（``vix_value_date`` 比 ``trade_date`` 早一天），本身滞后一日。

    即期望贡献在零附近且不稳健，而取数失败风险是确定的。保留 ``BLACK_SWAN``：实测
    触发的 3 天（06-23、06-26、07-02）benchmark 本来就已是 CRASH，留着不损失任何
    开仓机会，却保住一条真尾部风险的兜底。
    """
    benchmark_norm = normalize_regime(benchmark)
    premarket_norm = str(premarket or "").strip().upper()
    if premarket_norm not in PREMARKET_ESCALATION_REGIMES:
        return benchmark_norm
    return stricter_market_regime(benchmark_norm, premarket_norm)


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


def oms_buy_block_regimes() -> frozenset[str]:
    """下单闸门实际拦截的水温集合 —— 写入闸门与 OMS 闸门共用的唯一真源。

    以前 ``STEP4_BUY_BLOCK_REGIMES`` 只被 ``step4_order_config_from_env`` 读到，
    ``resolve_market_trade_mode``（写正式推荐的那一侧）压根不看它，于是运维把 NEUTRAL
    加进禁买名单后只有一半系统照办：2026-08 生产实况 write=True 而 OMS 禁买，
    ``recommendation_tracking`` 里躺着 16 行没有 ``:market_blocked`` 后缀的正式推荐
    （20260720×9 / 20260721×4 / 20260723×3，全为 l4_springboard、非 AI 推荐），
    用户在报告里看到「可执行买入」，实际一股也买不到。

    把解析收到这里，两侧就不可能再错位——凡是买不到的水温，就不写成正式推荐。
    ``STEP4_BUY_ALLOW_REGIMES`` 依旧同时豁免两侧，回退只需改一个变量。
    """
    import os

    raw = os.getenv("STEP4_BUY_BLOCK_REGIMES", _DEFAULT_OMS_BUY_BLOCK)
    values = {item.strip().upper() for item in raw.split(",") if item.strip() and item.strip().upper() != "COOLDOWN"}
    merged = values | set(EXECUTE_BLOCK_NEW_BUY_REGIMES)
    # PROBE 降档也必须在这里减掉，否则又是上面那种错位的镜像：写入侧按 PROBE 放行、
    # OMS 侧仍禁买，报告写「谨慎试探」而一股也买不到。降档水温买得少，但买得到。
    return frozenset(merged - _explicitly_allowed_regimes() - _probe_downgraded_regimes())


def _shadow_only_trade_mode(
    regime: str,
    *,
    mode: str,
    label: str,
    action: str,
    reason: str,
) -> MarketTradeMode:
    """禁正式推荐、禁新开、但保留 AI/shadow 对照的档位（权限完全一致，只有文案不同）。"""
    return MarketTradeMode(
        regime=regime,
        mode=mode,
        label=label,
        action=action,
        reason=reason,
        allow_ai_review=True,
        allow_recommendation_write=False,
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

    NEUTRAL 为何留在禁买（2026-08-31 重测，纠正旧口径）：旧注释引的「超额 -4.35pct」
    测的是 ``all`` 宽候选池（日均 94~145 只），不是闸门实际管辖的正式推荐。改测
    ``formal_l4`` 口径（8 个交易日，T+1 开盘买入，扣 0.202%，对照组用「T 日已知
    20 日涨幅最近邻 1:1 无放回配对」的非候选股，残差动量 +0.02pct）：

    | H  | 绝对收益 | 配对超额 | t     | 为正日 |
    |---:|--------:|--------:|------:|------:|
    |  5 |  -2.43% |  +6.99% | +2.41 |  6/8  |
    | 10 |  -6.19% |  +8.07% | +2.47 |  7/8  |
    | 20 |  -5.72% |  +4.74% | +1.43 |  5/8  |

    配对超额为正、绝对收益为负——选股相对同动量同侪有优势，但该动量本身在这段窗口
    亏钱（非闸门 H=10 动量十分位单调倒挂：D1 +2.9% → D10 -4.3%），对照组只是亏得更多。
    那 16 只真实推荐同向：H=20 绝对 -3.98%，且 16/16 全部出现两位数 MAE
    （-3.6% ~ -28.1%，均值约 -14%）。结论对、论证错，不因论证错而翻结论。
    """
    import os

    raw = os.getenv("STEP4_BUY_ALLOW_REGIMES", "").strip()
    return frozenset(item.strip().upper() for item in raw.split(",") if item.strip())


def _probe_downgraded_regimes() -> frozenset[str]:
    """读 STEP4_BUY_PROBE_REGIMES —— 把硬防守档降为 PROBE 档，而不是完全禁买。

    **为什么不复用 STEP4_BUY_ALLOW_REGIMES**：ALLOW 表示「完全豁免」，落到
    ``mainline_active``（全 L4 + 主题晋级 + 满配额）。硬防守档需要的是「小仓」而不是
    「满仓」，语义不同。且 ALLOW 对 NO_NEW_BUY 无效是**有意设计**，由
    ``test_hard_defense_regimes_ignore_allow_list`` 与
    ``test_defensive_regimes_never_openable_by_allow`` 两处钉住，不能当疏漏去补——
    补了等于把 CRASH/BLACK_SWAN/UNKNOWN 一起打开。故另开一个更窄的入口。

    **白名单只有 RISK_OFF**（``PROBE_DOWNGRADABLE_REGIMES``）。写死不读 env，是因为
    下面的依据只覆盖 RISK_OFF 一档：

    1. ``observe_only`` 那句 reason 原文是「回测全周期弱势，新开仓胜率不足」。拿全市场
       前瞻收益量两个面板窗口、配环移控制（保 run 结构，不是打散日期，故标签的连续段
       长度不变），**这句立论不成立**：

       | 窗口 | 天数 | h=1 | h=2 | h=3 | h=5 | h=10 |
       |------|-----:|----:|----:|----:|----:|-----:|
       | 2026-07-01..09-07 | 35 | -0.16 | -0.57 | -1.05 | -1.56 | -0.72 |
       | 2026-04-20..09-07 | 67 | **+0.53** | **+0.99** | **+0.25** | **+0.73** | **+1.26** |

       （禁新仓日均值 − 允许日均值，负值=闸门拦对了。）换个窗口符号整列翻转，环移 p
       从 0.147 到 0.977 无一显著，不重叠日相位扫描在 h=2/3/5 上符号不一致。即这个
       标签**没有携带关于市场强弱的稳定信息**。

    2. 那些被拦的天里市场有右尾：全市场中位数每个 h 都为正（+0.13/+0.29/+0.45/+0.50/
       +2.08），胜率 49.2~59.1%，p90 +5.25~+16.21，头部一成均值 +8.98~+25.41。
       即「熊市也有主线」在数据上站得住，那些天不是买不了。

    **为什么仍然默认关闭**（本函数默认返回空集）：同期被拦日的漏斗自选票，对照同日
    同流动性池、同 20 日动量（±3.0pct 逐只最近邻无放回替换，残差动量 +0.13~+0.15）
    并没有量到正 edge——h=3 差 8.37 点、h=5 差 5.49 点。但被拦日只有 14/13/12/12/7 天，
    全在 MIN_DAYS=20 门槛下，所以那是**尚未可知**而非「已证明更差」。开这道门的目的
    正是攒够这批天数：PROBE 档每天最多 1 只、禁 ATTACK、额度受
    ``STEP4_REPAIR_PROBE_BUDGET_LIMIT`` 限制，敞口远小于 ``mainline_active``。

    **CRASH/BLACK_SWAN/UNKNOWN 为何不进白名单**：上面量的是均值与胜率，**没有量左尾**。
    一个在均值上没判别力的标签仍可能在尾部有用，而这三档正是尾部兜底。UNKNOWN 另有
    独立理由——它同时是取数失败的兜底值（见 merge_premarket_regime），放开它等于让
    数据抖动变成开仓许可。

    回退：清空 STEP4_BUY_PROBE_REGIMES 即刻恢复禁买，两侧同时关闭。
    """
    import os

    raw = os.getenv("STEP4_BUY_PROBE_REGIMES", "").strip()
    requested = {item.strip().upper() for item in raw.split(",") if item.strip()}
    return frozenset(requested & PROBE_DOWNGRADABLE_REGIMES)


def probe_only_regimes() -> frozenset[str]:
    """当前实际按 PROBE 档限仓的水温集合 —— 限仓侧的唯一真源。

    在静态的 ``PROBE_ONLY_REGIMES`` 之外并入被 ``STEP4_BUY_PROBE_REGIMES`` 降档的
    硬防守水温。所有限仓判定都必须走本函数而不是那个常量：漏一处，降档后的水温就会
    掉到满配额分支（``max_new_buy_names`` 的 ``limits.neutral``），从「硬拦」直接跳成
    「满仓」——恰好是这次改动要避免的那个反面。
    """
    return frozenset(PROBE_ONLY_REGIMES | _probe_downgraded_regimes())


def _confirmation_only_trade_mode(regime: str, *, reason: str) -> MarketTradeMode:
    """「最多一只二次确认 PROBE」档（CAUTION 与降档后的硬防守档共用）。

    共用同一个构造器是刻意的：两者的权限位必须逐字一致，否则降档路径会悄悄拿到
    CAUTION 没有的权限。``mode`` 也刻意复用 ``confirmation_only`` —— 下游有十余处按
    mode 字符串分发（screen_tools、funnel_render、execution_playbook、candidate_actions
    的 readiness 映射），新增一个 mode 值会让这些分发全部落到 default，而
    ``candidate_action_info`` 对未注册状态的兜底是 ``blocks_direct_buy=False``（放行），
    静默走错的方向恰好是放松。只有 ``reason`` 按水温区分，供运维回溯。
    """
    return MarketTradeMode(
        regime=regime,
        mode="confirmation_only",
        label="谨慎试探",
        action="谨慎试探：最多一只二次确认后的 PROBE，禁止 ATTACK",
        reason=reason,
        allow_ai_review=True,
        allow_recommendation_write=True,
        allow_full_l4=False,
        allow_bypass_review=False,
        allow_theme_promotion=False,
    )


def _hard_defense_trade_mode(regime_norm: str) -> MarketTradeMode | None:
    """硬防守档的两种落点：降档小仓，或整层禁新仓。非硬防守档返回 None。

    抽成一支是为了让「降档」与「禁买」在同一处相邻可读——两者必须一起改，
    分散到主分发链里容易只动一半。注意顺序：降档判定要在 NO_NEW_BUY 之前，
    否则永远短路不到。
    """
    if regime_norm in _probe_downgraded_regimes():
        return _confirmation_only_trade_mode(
            regime_norm,
            reason=f"{regime_norm} 已按 STEP4_BUY_PROBE_REGIMES 降为小仓试探：只开放一只二次确认 PROBE",
        )
    if regime_norm not in NO_NEW_BUY_REGIMES:
        return None
    return MarketTradeMode(
        regime=regime_norm,
        mode="observe_only",
        label="禁止新仓",
        action="禁止新仓：仅影子观察，不送AI、不写推荐、不生成新买入",
        # 注意别写回「回测全周期弱势／新开仓胜率不足」：那句已被实测冲掉——67 天面板上
        # 禁新仓日的全市场胜率反而比允许日高 3~7 点，且换窗口符号整列翻转、环移 p
        # 全不显著（见 _probe_downgraded_regimes）。这一档现在的依据是「左尾未量过」
        # 的防守性保留，而不是已证明的弱势。
        reason=f"{regime_norm} 属硬防守档：左尾风险未经检验，默认不新开（可按 STEP4_BUY_PROBE_REGIMES 降为小仓）",
        allow_ai_review=False,
        allow_recommendation_write=False,
        allow_full_l4=False,
        allow_bypass_review=False,
        allow_theme_promotion=False,
    )


def resolve_market_trade_mode(regime: str | None) -> MarketTradeMode:
    regime_norm = normalize_regime(regime)
    allowed = _explicitly_allowed_regimes()
    hard_defense = _hard_defense_trade_mode(regime_norm)
    if hard_defense is not None:
        return hard_defense
    if regime_norm in OVERHEAT_SHADOW_REGIMES and regime_norm not in allowed:
        return _shadow_only_trade_mode(
            regime_norm,
            mode="overheat_shadow",
            label="禁止新仓",
            action="禁止新仓：可送AI/shadow 对照，不写正式推荐、不执行新买入",
            reason="RISK_ON 过热追新历史负期望；保留研究样本，禁止正式下单",
        )
    if regime_norm in REPAIR_REVIEW_REGIMES and regime_norm not in allowed:
        return _shadow_only_trade_mode(
            regime_norm,
            mode="repair_review",
            label="修复观察（禁止新仓）",
            action="仅研究修复候选；不写正式推荐、不生成新仓订单",
            reason=f"{regime_norm} 只适合验证修复强度，禁止自动开仓",
        )
    if regime_norm in REPAIR_PROBE_REGIMES:
        return _confirmed_repair_trade_mode(regime_norm)
    if regime_norm in CAUTION_ONLY_REGIMES:
        return _confirmation_only_trade_mode(
            regime_norm,
            reason="情绪扰动期只开放小额试探，候选必须经过确认支撑",
        )
    if regime_norm in oms_buy_block_regimes():
        # 运维把这一档加进了 STEP4_BUY_BLOCK_REGIMES：买不到就别写成正式推荐。
        # 保留 AI/shadow 以便继续攒对照样本（shadow ledger 跟 allow_ai_review，
        # 不跟着 allow_recommendation_write 一起停）。
        return _shadow_only_trade_mode(
            regime_norm,
            mode="execution_blocked",
            label="禁止新仓",
            action="禁止新仓：可送AI/shadow 对照，不写正式推荐、不执行新买入",
            reason=f"{regime_norm} 在下单闸门禁买名单内，正式推荐随之关闭以免与实际持仓不符",
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
