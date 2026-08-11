"""Step3 track-level LLM user-message builders."""

from __future__ import annotations

from core.market_trade_mode import normalize_regime


def _track_execution_requirements() -> str:
    return (
        "补充执行要求：\n"
        "1) 买入触发必须包含量价确认条件（缩量回踩/拒绝下破）；放量下破必须取消买入。\n"
        "2) 盘面解剖须结合振幅、收位与量比，说明洗盘/承接/冲高回落的博弈痕迹。\n"
        "3) 【板块状态/证据】仅作行业参考，最终以个股量价结构定生死。\n"
        "4) 【结构支撑/阻力】中的 Creek 是箱体上沿，Ice 是箱体下沿；突破 Creek 后不能回落，跌破 Ice 后必须快速收回才可视作 Spring。\n"
        "5) 【起跳板预判】A=缩量高收测试，B=放量高收突破，C=支撑多次测试；若事实切片冲突，以事实切片为准。\n"
        "6) 若同时出现【退出预警】和向上异动，默认按诱多/修复失败审查，除非重新站回关键位且放量高收。\n"
        "7) 近15日切片后的 VSA 标签仅是辅助索引，最终仍必须引用原始涨跌、振幅、收位与量比。\n\n"
        "8) 【交易闸门】优先于量价评分：只有跨日状态=confirmed（VALIDATED）的标的才允许进入“处于起跳板”。\n"
        "9) SURVIVED 仅表示未失效；即使满足 A/B/C，也只能写入储备营地。\n"
        "10) VALIDATED 只由上游状态机产生，OMS 只做最终核准。\n\n"
    )


def _track_scope_text(track_key: str, regime_upper: str) -> str:
    if track_key == "Trend":
        scope = (
            "[本轮分析范围]\n"
            "本轮仅分析 Trend轨（右侧主升 / 放量点火 / 突破组）。\n"
            "请重点审查是否存在高潮诱多、深水区反抽、爆量次日承接不足，以及看似突破实为派发等问题。"
        )
        if regime_upper == "CRASH":
            scope += (
                "\n\u26a0\ufe0f 当前 CRASH 环境，右侧突破全部视为诱多。\n"
                "Trend 轨所有标的一律归入逻辑破产或储备营地，不得放入起跳板。"
            )
        elif regime_upper == "RISK_OFF":
            scope += (
                "\n\u26a0\ufe0f 当前大盘处于弱势环境，右侧假突破概率极高。\n"
                "Trend 信号必须有突破日量比 >= 1.5x 且次日承接不回落，否则视为诱多归入逻辑破产。"
            )
        return scope
    scope = (
        "[本轮分析范围]\n"
        "本轮仅分析 Accum轨（左侧潜伏 / Spring / LPS / Accum_C 组）。\n"
        "请重点审查供应是否真正枯竭；若下跌放量或支撑反复失守，应归入逻辑破产或储备营地。\n"
        "若出现长下影、高收位、放量拉回，不得机械判死刑，必须分辨是真Spring还是失败反抽。"
    )
    if regime_upper in ("RISK_OFF", "CRASH"):
        scope += (
            "\n\u26a0\ufe0f 当前大盘处于弱势环境，左侧抄底风险极高。\n"
            "Accum 信号必须同时满足：1) 缩量测试量比 < 0.6x 2) 支撑位至少 2 次测试未破。\n"
            "不满足的一律归入储备营地，不得放入起跳板。"
        )
    return scope


def _regime_hint_text(regime_upper: str) -> str:
    if regime_upper == "CRASH":
        return "[仓位约束] 当前 CRASH 环境，禁止推荐起跳板，全部归入储备营地或逻辑破产。\n\n"
    if regime_upper == "RISK_OFF":
        return "[仓位约束] 当前 RISK_OFF 弱势环境，起跳板最多 1-2 只，必须有极强的量价确认。\n\n"
    if regime_upper == "RISK_ON":
        return "[仓位约束] 当前 RISK_ON 追涨期，反转率高，起跳板最多 2 只，必须有缩量回踩确认。\n\n"
    return ""


def _candidate_compression_text(compressed: bool, raw_count: int, selected_count: int) -> str:
    if compressed and raw_count > selected_count:
        return f"[候选说明] 本轮候选已从 {raw_count} 只压缩到 {selected_count} 只。\n\n"
    return ""


def _track_distribution_instructions() -> str:
    return (
        "以下是本轮候选名单。\n"
        "请做三阵营分流：1) 逻辑破产 2) 储备营地 3) 处于起跳板。\n"
        "其中前两类属于非操作区，第三类只是可送 OMS 复核的候选区，不等于买入订单。\n"
        "交易闸门硬规则：只有跨日状态=confirmed（VALIDATED）的标的才允许进入第三类；"
        "SURVIVED/pending 只能进入储备营地或逻辑破产。\n"
        "每只股票只写一行：`代码 名称 | 状态 | A/B/C | 核心证据 | 升级条件 | 失效条件`；"
        "不足两项不得进入起跳板。\n"
        "所有买入动作必须等待下游 OMS 输出 BUY-APPROVED，禁止在本报告中写仓位、股数或买入金额。\n"
        "输出必须包含这三个部分，且只能使用输入列表中的股票代码，不得遗漏或新增。\n\n"
    )


def build_track_user_message(
    track: str,
    benchmark_lines: list[str],
    payloads: list[str],
    *,
    compressed: bool,
    raw_count: int,
    selected_count: int,
    regime: str = "",
) -> str:
    """构建发送给 LLM 的轨道级用户消息。"""
    track_key = "Accum" if str(track).strip() == "Accum" else "Trend"
    regime_upper = normalize_regime(regime)
    return (
        ("{}\n\n".format("\n".join(benchmark_lines)) if benchmark_lines else "")
        + _regime_hint_text(regime_upper)
        + f"{_track_scope_text(track_key, regime_upper)}\n\n"
        + _candidate_compression_text(compressed, raw_count, selected_count)
        + _track_distribution_instructions()
        + _track_execution_requirements()
        + "\n".join(payloads)
    )
