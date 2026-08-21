"""Step4 market guardrail composition."""

from __future__ import annotations

import logging

from core.market_trade_mode import (
    PREMARKET_DATA_GAP,
    normalize_regime,
    stricter_market_regime,
)
from integrations.supabase_market_signal import compose_market_banner, load_market_signal_daily, market_signal_readiness
from workflows.step4_text import clean_text

logger = logging.getLogger(__name__)

PREMARKET_REGIMES = frozenset({"UNKNOWN", "NORMAL", "CAUTION", "RISK_OFF", "BLACK_SWAN"})
PREMARKET_GAP = "premarket"
BENCHMARK_GAP = "benchmark"


def normalize_benchmark_regime(raw: object) -> str:
    return normalize_regime(clean_text(raw))


def normalize_premarket_regime(raw: object) -> str:
    regime = clean_text(raw).upper()
    if regime in PREMARKET_REGIMES:
        return regime
    return "UNKNOWN"


def resolve_effective_market_regime(benchmark_regime: object, premarket_regime: object) -> str:
    """收盘态与盘前态取更严者。

    ``premarket`` **完全缺失**（None/空串）时不再拉成 UNKNOWN，而是回落到 benchmark
    单独判定——「上游任务没跑」是系统故障，不是市场信号，把两者混为一谈会让取数失败
    冒充风险事件。真实的 ``"UNKNOWN"`` 判定与拼写错误仍然 fail-closed：前者是盘前
    模型明确给出的「看不清」，后者说明数据不可信，两种都该收紧。

    实测 60 个交易日里，2026-07-20 与 07-21 两天 benchmark 为 NEUTRAL（放行）却因
    premarket 缺失被降级为 UNKNOWN 而禁买。
    """
    benchmark_norm = normalize_benchmark_regime(benchmark_regime)
    raw = clean_text(premarket_regime).upper()
    # 两类「拿不到数据」都回落：字段为空（盘前任务没跑）与 DATA_GAP（任务跑了但
    # A50/VIX 取数失败）。此前只处理前者，导致「跑了反而更严格」的不一致。
    if not raw or raw == PREMARKET_DATA_GAP:
        return benchmark_norm
    premarket_norm = normalize_premarket_regime(premarket_regime)
    return stricter_market_regime(benchmark_norm, premarket_norm)


def load_market_signal_for_trade_date(trade_date: str) -> dict[str, object] | None:
    try:
        return load_market_signal_daily(trade_date)
    except Exception as e:
        logger.warning("读取 market_signal_daily 失败: trade_date=%s, err=%s", trade_date, e)
        return None


def _benchmark_regime_and_readiness(
    row: dict[str, object], benchmark_context: dict | None, trade_date: str
) -> tuple[str, dict[str, str]]:
    readiness = market_signal_readiness(row, trade_date)
    context_regime = (benchmark_context or {}).get("regime")
    regime = normalize_benchmark_regime(context_regime or row.get("benchmark_regime"))
    if not context_regime and readiness["status"] in {"missing", "stale"}:
        regime = "UNKNOWN"
    return regime, readiness


def missing_market_inputs(
    raw_benchmark: object,
    raw_premarket: object,
    readiness: dict[str, str],
    benchmark_context: dict | None,
) -> list[str]:
    """列出「本该有却没拿到」的风控输入，返回稳定标识。

    `normalize_*_regime` 会把缺失值和真实的 UNKNOWN 判定压成同一个 UNKNOWN，
    而 UNKNOWN 属于禁止开仓状态。不单独识别缺失，运维就无法区分「行情不明」和
    「上游任务没跑」——生产 47 天里有 11 天因后者被禁买，比 NEUTRAL 放行的天数还多。
    """
    missing: list[str] = []
    if not clean_text(raw_premarket):
        missing.append(PREMARKET_GAP)
    benchmark_unusable = not clean_text(raw_benchmark) or readiness["status"] in {"missing", "stale"}
    if benchmark_unusable and not (benchmark_context or {}).get("regime"):
        missing.append(BENCHMARK_GAP)
    return missing


def describe_market_gaps(gaps: list[str]) -> list[str]:
    labels = {
        PREMARKET_GAP: "盘前风控未产出（premarket_risk 工作流当日未运行）",
        BENCHMARK_GAP: "收盘基准未就绪（market_signal_daily 缺 benchmark_regime）",
    }
    return [labels[gap] for gap in gaps if gap in labels]


def data_gap_blocks_buying(benchmark_regime: str, gaps: list[str], enforced_blocks: set[str]) -> bool:
    """缺失项是否就是禁买的决定性原因。

    收盘态自身已经禁买时（例如 CRASH），补齐盘前数据也不会放行，此时把禁买归因于
    数据缺失会误导运维去补一个于事无补的任务。
    """
    if not gaps:
        return False
    if BENCHMARK_GAP in gaps:
        return True
    return resolve_effective_market_regime(benchmark_regime, "NORMAL") not in enforced_blocks


def _guardrail_report_lines(
    *,
    trade_date: str,
    effective_regime: str,
    benchmark_regime: str,
    premarket_regime: str,
    readiness: dict[str, str],
    benchmark_context: dict | None,
    missing_inputs: list[str],
    enforced_blocks: set[str],
    panic_reasons: list[str],
    premarket_reasons: list[str],
) -> list[str]:
    lines = [
        "[全局风控]",
        f"trade_date={trade_date}, effective_regime={effective_regime}, "
        f"benchmark_regime={benchmark_regime}, premarket_regime={premarket_regime}",
        f"market_data_status={readiness['status']}, reason={readiness['reason']}",
    ]
    if benchmark_context:
        lines.append(
            f"benchmark_close={benchmark_context.get('close')}, ma50={benchmark_context.get('ma50')}, "
            f"ma200={benchmark_context.get('ma200')}, recent3={benchmark_context.get('recent3_pct')}, "
            f"cum3={benchmark_context.get('recent3_cum_pct')}, smallcap_today={benchmark_context.get('smallcap_today_pct')}"
        )
    if effective_regime in enforced_blocks:
        lines.append("⚠️ 全局风控一票否决：OMS 将强制拦截全部买入动作（仅允许 HOLD/TRIM/EXIT）。")
        if missing_inputs:
            lines.append(
                "⛑️ 本次禁买源自数据缺失而非行情判断，缺失项="
                + "、".join(missing_inputs)
                + "。补齐后重跑 Step4 即可恢复开仓能力。"
            )
    elif premarket_regime == "CAUTION":
        lines.append("⚠️ 盘前情绪扰动已触发：OMS 会自动收紧追价阈值并优先防守。")
    if panic_reasons:
        lines.append("panic_reasons=" + " | ".join(panic_reasons))
    if premarket_reasons:
        lines.append("premarket_reasons=" + " | ".join(premarket_reasons))
    lines.append("")
    return lines


def _merge_live_benchmark(
    row: dict[str, object],
    benchmark_context: dict | None,
    benchmark_regime: str,
    premarket_regime: str,
) -> None:
    if benchmark_context:
        row.update(
            {
                "benchmark_regime": benchmark_regime,
                "main_index_close": benchmark_context.get("close"),
                "main_index_ma50": benchmark_context.get("ma50"),
                "main_index_ma200": benchmark_context.get("ma200"),
                "main_index_recent3_cum_pct": benchmark_context.get("recent3_cum_pct"),
                "main_index_today_pct": benchmark_context.get("main_today_pct"),
                "smallcap_close": benchmark_context.get("smallcap_close"),
                "smallcap_recent3_cum_pct": benchmark_context.get("smallcap_recent3_cum_pct"),
            }
        )
    row["premarket_regime"] = premarket_regime


def _premarket_label_for_report(raw_premarket: object) -> str:
    """报告用盘前标签：保留 DATA_GAP/空缺，勿在 resolve 之前压成 UNKNOWN。"""
    raw = clean_text(raw_premarket).upper()
    if not raw:
        return ""
    if raw == PREMARKET_DATA_GAP:
        return PREMARKET_DATA_GAP
    return normalize_premarket_regime(raw_premarket)


def build_market_guardrail(
    *,
    trade_date: str,
    benchmark_context: dict | None,
    market_signal_row: dict[str, object] | None,
    buy_block_regimes: set[str],
) -> tuple[str, str, str]:
    row = dict(market_signal_row or {})
    benchmark_regime, readiness = _benchmark_regime_and_readiness(row, benchmark_context, trade_date)
    # 必须把原始盘前值交给 resolve：normalize 会把 DATA_GAP/空值压成 UNKNOWN，
    # 从而让「取数失败 / 任务没跑 → 回落 benchmark」的修复在生产入口失效。
    raw_premarket = row.get("premarket_regime")
    effective_regime = resolve_effective_market_regime(benchmark_regime, raw_premarket)
    premarket_regime = _premarket_label_for_report(raw_premarket)
    # buy_block_regimes 已是 EXECUTE∪BLOCK−ALLOW；不得再并回 EXECUTE，否则
    # STEP4_BUY_ALLOW_REGIMES 豁免会被横幅/提示文案重新否决。
    enforced_blocks = set(buy_block_regimes)
    gaps = missing_market_inputs(row.get("benchmark_regime"), raw_premarket, readiness, benchmark_context)
    # 只有真正因此禁买时才归因于数据缺失，避免回落后已放行仍写「禁买源自…」。
    missing_inputs = (
        describe_market_gaps(gaps)
        if effective_regime in enforced_blocks and data_gap_blocks_buying(benchmark_regime, gaps, enforced_blocks)
        else []
    )
    if missing_inputs:
        logger.warning(
            "风控输入缺失导致降级为 %s（禁止新开仓）: trade_date=%s, 缺失=%s",
            effective_regime,
            trade_date,
            "、".join(missing_inputs),
        )

    _merge_live_benchmark(row, benchmark_context, benchmark_regime, premarket_regime)
    banner = compose_market_banner(row)
    panic_reasons = [
        str(x).strip() for x in ((benchmark_context or {}).get("panic_reasons", []) or []) if str(x).strip()
    ]
    premarket_reasons = [str(x).strip() for x in (row.get("premarket_reasons", []) or []) if str(x).strip()]

    lines = _guardrail_report_lines(
        trade_date=trade_date,
        effective_regime=effective_regime,
        benchmark_regime=benchmark_regime,
        premarket_regime=premarket_regime or "(missing)",
        readiness=readiness,
        benchmark_context=benchmark_context,
        missing_inputs=missing_inputs,
        enforced_blocks=enforced_blocks,
        panic_reasons=panic_reasons,
        premarket_reasons=premarket_reasons,
    )

    posture_name = clean_text(banner.get("market_posture_name"))
    action_phrase = clean_text(banner.get("action_phrase"))
    system_market_view = f"系统风控：{effective_regime}"
    if posture_name:
        system_market_view += f" / {posture_name}"
    view_parts = [f"收盘={benchmark_regime}"]
    if premarket_regime and premarket_regime != "NORMAL":
        view_parts.append(f"盘前={premarket_regime}")
    if missing_inputs:
        view_parts.append("⛑️ 禁买源自数据缺失：" + "、".join(missing_inputs))
    if action_phrase:
        view_parts.append(action_phrase)
    if view_parts:
        system_market_view += " | " + "；".join(view_parts)

    return (effective_regime, "\n".join(lines), system_market_view)
