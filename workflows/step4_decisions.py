"""Step4 decision completion, market-data backfill, and execution."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

from integrations.fetch_a_share_csv import TradingWindow
from workflows.step4_decision_parser import trim_new_buy_decisions
from workflows.step4_models import (
    CandidateMeta,
    DecisionItem,
    ExecutionTicket,
    PortfolioState,
    Step4InputContext,
    Step4OrderConfig,
    Step4RuntimeConfig,
)
from workflows.step4_order_engine import WyckoffOrderEngine
from workflows.step4_payload import calc_atr, fetch_latest_real_close, load_qfq_history

logger = logging.getLogger(__name__)

_BLOCKING_CANDIDATE_ACTION_STATUSES = {
    "watch_only",
    "blocked_by_data_quality",
    "blocked_by_market_gate",
    "blocked_by_policy_guard",
    "repair_review_only",
    "confirmation_required",
}


def rendered_step4_market_view(system_market_view: str, model_market_view: str) -> str:
    if model_market_view and system_market_view:
        return f"{system_market_view} | 模型摘要：{model_market_view}"
    return model_market_view or system_market_view


def complete_step4_decisions(
    decisions: list[DecisionItem],
    portfolio: PortfolioState,
    candidate_meta_map: dict[str, CandidateMeta],
    market_regime: str,
    runtime_config: Step4RuntimeConfig,
) -> list[DecisionItem]:
    mentioned_codes = {d.code for d in decisions}
    for position in portfolio.positions:
        if position.code in mentioned_codes:
            continue
        decisions.append(
            DecisionItem(
                code=position.code,
                name=position.name,
                action="HOLD",
                entry_zone_min=None,
                entry_zone_max=None,
                stop_loss=None,
                trim_ratio=None,
                tape_condition="默认观察",
                invalidate_condition="",
                is_add_on=True,
                reason="模型未给出动作，系统默认 HOLD",
                confidence=None,
            )
        )
    decisions = _attach_candidate_meta(decisions, candidate_meta_map)
    decisions = _limit_high_drawdown_attacks(decisions, candidate_meta_map)
    held_codes = {p.code for p in portfolio.positions}
    decisions = _limit_ai_candidate_upgrades(decisions, held_codes)
    kept_decisions, dropped, max_new_names = trim_new_buy_decisions(
        decisions,
        held_codes=held_codes,
        market_regime=market_regime,
        limits=runtime_config.new_buy_limits,
    )
    if dropped:
        logger.info(
            "组合级限购生效: regime=%s, max_new_buy_names=%s, dropped=%s",
            market_regime,
            max_new_names,
            ",".join(dropped),
        )
    return _append_rejected_new_buys(
        kept_decisions,
        decisions,
        dropped,
        market_regime=market_regime,
        max_new_names=max_new_names,
    )


def _limit_ai_candidate_upgrades(
    decisions: list[DecisionItem],
    held_codes: set[str],
) -> list[DecisionItem]:
    out: list[DecisionItem] = []
    for decision in decisions:
        if decision.action != "ATTACK" or decision.code in held_codes:
            out.append(decision)
            continue
        reason = "AI候选审计不得把外部新仓升级为ATTACK"
        detail = f"{decision.reason}；{reason}" if decision.reason else reason
        out.append(replace(decision, action="PROBE", reason=detail))
    return out


# 「极高波动」是改名前的标签；库里与在途候选可能仍带它，漏配会让 OMS 少做一次降级。
_DRAWDOWN_RISK_MARKERS = re.compile(r"60日(?:高波动|深回撤|极高波动)")


def _limit_high_drawdown_attacks(
    decisions: list[DecisionItem],
    candidate_meta_map: dict[str, CandidateMeta],
) -> list[DecisionItem]:
    out: list[DecisionItem] = []
    for decision in decisions:
        meta = candidate_meta_map.get(decision.code)
        high_drawdown = meta and any(_DRAWDOWN_RISK_MARKERS.search(risk) for risk in meta.risk_factors)
        if decision.action != "ATTACK" or not high_drawdown:
            out.append(decision)
            continue
        reason = "趋势60日回撤显示波动与下行偏大，只允许PROBE试仓"
        detail = f"{decision.reason}；{reason}" if decision.reason else reason
        out.append(replace(decision, action="PROBE", reason=detail))
    return out


def backfill_step4_decision_market_data(
    decisions: list[DecisionItem],
    window: TradingWindow,
    latest_price_map: dict[str, float],
    atr_map: dict[str, float],
    runtime_config: Step4RuntimeConfig,
) -> None:
    missing_codes = [
        decision.code
        for decision in decisions
        if not decision.system_reject_reason and decision.code not in latest_price_map
    ]
    if not missing_codes:
        return
    with ThreadPoolExecutor(max_workers=runtime_config.max_workers) as executor:
        futures = {
            executor.submit(_fetch_step4_decision_market_data, code, window, runtime_config): code
            for code in missing_codes
        }
        for future in as_completed(futures):
            code, atr_v, px = future.result()
            if atr_v is not None:
                atr_map[code] = atr_v
            if px is not None:
                latest_price_map[code] = px


def execute_step4_decisions(
    context: Step4InputContext,
    decisions: list[DecisionItem],
    order_config: Step4OrderConfig,
    stale_exit_codes: frozenset[str] = frozenset(),
) -> tuple[list[ExecutionTicket], float]:
    codes = {p.code for p in context.portfolio.positions} | {d.code for d in decisions}
    engine = WyckoffOrderEngine(
        total_equity=float(context.total_equity),
        free_cash=context.portfolio.free_cash,
        position_map={p.code: p for p in context.portfolio.positions},
        latest_price_map=context.latest_price_map,
        atr_map=context.atr_map,
        market_regime=context.market_regime,
        config=order_config,
        trade_date=context.trade_date,
        stale_exit_codes=stale_exit_codes,
        cny_rates=_load_step4_cny_rates(codes),
    )
    return engine.process(decisions)


def _load_step4_cny_rates(codes: set[str]) -> dict[str, float]:
    """OMS 现金与预算按人民币计；港美报价需汇率。取不到时只给 CNY=1，外币交易会因缺汇率拒单。"""
    from core.portfolio_valuation import portfolio_currency
    from integrations.portfolio_market_value import load_cny_rates

    currencies = {portfolio_currency(code) for code in codes if code}
    if currencies <= {"CNY"}:
        return {"CNY": 1.0}
    try:
        return load_cny_rates(currencies)
    except Exception:
        return {"CNY": 1.0}


def _attach_candidate_meta(
    decisions: list[DecisionItem],
    meta_map: dict[str, CandidateMeta],
) -> list[DecisionItem]:
    out: list[DecisionItem] = []
    for dec in decisions:
        meta = meta_map.get(dec.code)
        if not meta:
            out.append(dec)
            continue
        out.append(
            replace(
                dec,
                wyckoff_track=meta.track or dec.wyckoff_track,
                wyckoff_stage=meta.stage or dec.wyckoff_stage,
                wyckoff_tag=meta.tag or dec.wyckoff_tag,
                funnel_score=meta.funnel_score if dec.funnel_score is None else dec.funnel_score,
                source_type=meta.source_type or dec.source_type,
                capital_migration_bonus=(
                    meta.capital_migration_bonus if dec.capital_migration_bonus is None else dec.capital_migration_bonus
                ),
                system_reject_reason=dec.system_reject_reason or _candidate_buy_guard_reason(dec, meta),
            )
        )
    return out


def _candidate_buy_guard_reason(dec: DecisionItem, meta: CandidateMeta) -> str:
    if dec.action not in {"PROBE", "ATTACK"}:
        return ""
    if meta.label_ready is False:
        return _candidate_guard_reason("候选标签未成熟，禁止直接买入", meta)
    if meta.new_buy_allowed is False:
        return _candidate_guard_reason("候选未开放新增买入，禁止直接买入", meta)
    if meta.trade_readiness in {"research_only", "review_only"}:
        return _candidate_guard_reason(f"候选交易就绪状态 {meta.trade_readiness} 不允许直接买入", meta)
    status = meta.action_status.strip()
    if status.startswith("blocked_") or status in _BLOCKING_CANDIDATE_ACTION_STATUSES:
        return _candidate_guard_reason(f"候选状态 {status} 不允许直接买入", meta)
    return ""


def _candidate_guard_reason(base: str, meta: CandidateMeta) -> str:
    parts = [base]
    if meta.action_status:
        parts.append(f"action_status={meta.action_status}")
    if meta.trade_readiness:
        parts.append(f"trade_readiness={meta.trade_readiness}")
    if meta.new_buy_allowed is False:
        parts.append("new_buy_allowed=false")
    if meta.risk_factors:
        parts.append("risk=" + "；".join(meta.risk_factors[:3]))
    if meta.next_step:
        parts.append(f"next_step={meta.next_step}")
    return "候选护栏拦截: " + " | ".join(parts)


def _append_rejected_new_buys(
    kept_decisions: list[DecisionItem],
    all_decisions: list[DecisionItem],
    dropped_codes: list[str],
    *,
    market_regime: str,
    max_new_names: int,
) -> list[DecisionItem]:
    if not dropped_codes:
        return kept_decisions
    dropped_set = set(dropped_codes)
    reason = f"组合级限购拦截: regime={market_regime}, max_new_buy_names={max_new_names}"
    rejected = [
        replace(dec, system_reject_reason=reason)
        for dec in all_decisions
        if dec.code in dropped_set and dec.action in {"PROBE", "ATTACK"}
    ]
    return kept_decisions + rejected


def _fetch_step4_decision_market_data(
    code: str,
    window: TradingWindow,
    runtime_config: Step4RuntimeConfig,
) -> tuple[str, float | None, float | None]:
    atr_v = None
    try:
        df_qfq = load_qfq_history(
            code,
            window,
            enforce_target_trade_date=runtime_config.enforce_target_trade_date,
        )
        atr_v = calc_atr(df_qfq, runtime_config.atr_period)
    except Exception as e:
        logger.warning("%s ATR 计算异常: %s", code, e)
    latest_close = fetch_latest_real_close(
        code,
        window,
        enforce_target_trade_date=runtime_config.enforce_target_trade_date,
    )
    return code, atr_v, latest_close
