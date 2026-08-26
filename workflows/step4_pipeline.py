"""Step4 OMS scheduling workflow shared by daily jobs and manual reruns."""

from __future__ import annotations

import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from core.a_share_entry_research import normalized_signal_type
from integrations.fetch_a_share_csv import resolve_trading_window
from integrations.supabase_portfolio import load_portfolio_state
from tools.report_parser import extract_invalidated_codes
from utils.env import env_bool
from utils.trading_clock import resolve_end_calendar_day
from workflows.step4_rebalancer import run as run_step4

TZ = ZoneInfo("Asia/Shanghai")

STEP4_REASON_MAP = {
    "missing_api_key": "Step4 LLM API Key 缺失",
    "skipped_invalid_portfolio": "用户持仓缺失或格式错误，已跳过",
    "skipped_notify_unconfigured": "OMS 通知通道未配置，已跳过",
    "skipped_idempotency": "今日已运行，已跳过",
    "skipped_no_decisions": "模型未给出有效决策，已跳过",
    "llm_failed": "Step4 模型调用失败",
    "notification_failed": "OMS 通知推送失败",
    "ok": "ok",
}

_CONFIRMED_STATUS_VALUES = {"confirmed", "已确认", "确认", "二次确认", "跨日确认"}
_CONFIRMED_SOURCE_VALUES = {"signal_confirmed", "二次确认", "跨日确认"}
_UNCONFIRMED_MARKERS = (
    "unconfirmed",
    "not_confirmed",
    "not confirmed",
    "confirmation_required",
    "pending",
    "observe",
    "observation",
    "watch_only",
    "未确认",
    "待确认",
    "观察",
)
_CONFIRMED_TAG_PATTERNS = (
    re.compile(r"^(?:confirmed|已确认|确认|二次确认|跨日确认)$", re.IGNORECASE),
    re.compile(r"^[a-z0-9_-]+[（(](?:(?:二次|跨日)?确认)[）)]$", re.IGNORECASE),
    re.compile(r"^[a-z0-9_-]+(?:二次|跨日)确认(?:[（(][^）)]*[）)])?$", re.IGNORECASE),
    re.compile(r"^主线买点确认(?:\s*[|｜].*)?$"),
)
_AI_CANDIDATE_POLICIES = {"shadow", "veto_only"}
_RULE_BLOCKED_ACTIONS = {
    "watch_only",
    "repair_review_only",
    "confirmation_required",
    "blocked_by_data_quality",
    "blocked_by_market_gate",
    "blocked_by_policy_guard",
    "blocked_by_quality_gate",
}
_RULE_BLOCKED_READINESS = {"research_only", "review_only", "observe_only"}


def now_text() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def log_line(msg: str, logs_path: str | None = None) -> None:
    line = f"[{now_text()}] {msg}"
    print(line, flush=True)
    if logs_path:
        os.makedirs(os.path.dirname(logs_path) or ".", exist_ok=True)
        with open(logs_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def latest_trade_date_str() -> str:
    window = resolve_trading_window(end_calendar_day=resolve_end_calendar_day(), trading_days=30)
    return window.end_trade_date.isoformat()


def load_step4_target() -> tuple[dict | None, str]:
    target_user_id = os.getenv("SUPABASE_USER_ID", "").strip()
    if not target_user_id:
        return None, "SUPABASE_USER_ID 未配置"
    portfolio_id = f"USER_LIVE:{target_user_id}"
    state = load_portfolio_state(portfolio_id)
    has_env_fallback = bool(os.getenv("MY_PORTFOLIO_STATE", "").strip())
    if not isinstance(state, dict) and not has_env_fallback:
        return None, f"未匹配到 user_id={target_user_id} 的持仓（{portfolio_id}）"
    return {"user_id": target_user_id, "portfolio_id": portfolio_id}, (
        "ok_supabase" if isinstance(state, dict) else "ok_env_fallback"
    )


def is_confirmed_step4_candidate(item: dict) -> bool:
    state_values = [
        str(item.get(field) or "").strip().lower()
        for field in (
            "status",
            "signal_status",
            "confirm_status",
            "confirmation_status",
            "candidate_status",
            "tag",
            "recommend_reason",
        )
    ]
    if any(marker in value for value in state_values for marker in _UNCONFIRMED_MARKERS):
        return False
    if item.get("is_confirmed") is True:
        return True
    status_values = {
        str(item.get(field) or "").strip().lower()
        for field in ("status", "signal_status", "confirm_status", "confirmation_status")
    }
    if status_values & _CONFIRMED_STATUS_VALUES:
        return True
    source = str(item.get("selection_source") or "").strip().lower()
    if source in _CONFIRMED_SOURCE_VALUES:
        return True
    for field in ("tag", "recommend_reason"):
        text = str(item.get(field) or "").strip()
        if any(pattern.fullmatch(text) for pattern in _CONFIRMED_TAG_PATTERNS):
            return True
    return False


def step4_ai_candidate_policy() -> str:
    """LLM 研报对买入候选的权限。

    默认 ``shadow``：研报只供人读，不参与拦截。改为 ``veto_only`` 才允许 LLM
    从已通过规则护栏的候选里否决标的。

    2026-08-08 起默认从 ``veto_only`` 改为 ``shadow``：LLM 输出受模型选型漂移与
    采样随机性影响，无法回测复现；且 recommendation_tracking 42 天内
    is_ai_recommended 仅 4 条、rag_vetoed 全为 0，说明该拦截既极少生效也未留下
    可评估记录。规则护栏（confirmed）始终是真正的硬门槛，关闭否决权不放开任何
    风险闸门。待否决记录落库并积累样本后再决定是否恢复或彻底移除。
    """
    policy = os.getenv("STEP4_AI_CANDIDATE_POLICY", "shadow").strip().lower()
    return policy if policy in _AI_CANDIDATE_POLICIES else "shadow"


def _is_false_like(value: object) -> bool:
    if value is False or value == 0:
        return True
    return isinstance(value, str) and value.strip().lower() in {"false", "0", "no", "n", "off"}


def _blocked_buy_signal_types() -> frozenset[str]:
    """按信号类型禁买。留空=不过滤（默认，保持原行为）。

    生产回测（scripts/backtest_runner.py，66 个交易日 / 124 笔 / 含真实成本）按买点归因：

        evr   59 笔  -2.30%  胜率 45.8%
        sos   24 笔  -4.92%  胜率 33.3%（中位 -11.04%）
        spring 40 笔 +0.05%  胜率 57.5%

    变体对比同区间（G = 剔除 evr 与 sos）在每个指标上都优于基线 A：
    总收益 -15.82% -> -6.31%、最大回撤 -23.91% -> -13.14%、夏普 -2.066 -> -1.207、
    胜率 47.97% -> 50.00%。机制上是把 Trend 成交从 82 笔压到 28 笔，席位让给 Accum
    （Accum 41 -> 78 笔），而 Trend 整体 -3.62% / Accum +1.19%。

    默认仍为空：core/a_share_entry_research.py 的注释要求「生产采用需跨周期与
    walk-forward 证据」，而上述证据只覆盖 2026-05~08 单段行情。故做成显式开关，
    由运维在拿到跨周期证据后开启，而不是改默认行为悄悄生效。
    """
    raw = os.getenv("STEP4_BLOCK_BUY_SIGNAL_TYPES", "").strip()
    return frozenset(normalized_signal_type(item) for item in raw.split(",") if item.strip())


def _rule_eligible_step4_candidate(item: dict) -> bool:
    if not is_confirmed_step4_candidate(item):
        return False
    if _is_false_like(item.get("new_buy_allowed")) or _is_false_like(item.get("label_ready")):
        return False
    blocked_signals = _blocked_buy_signal_types()
    if blocked_signals:
        signal = normalized_signal_type(item.get("signal_type") or item.get("trigger"))
        if signal in blocked_signals:
            return False
    readiness = str(item.get("trade_readiness") or "").strip().lower()
    if readiness in _RULE_BLOCKED_READINESS:
        return False
    action = str(item.get("action_status") or "").strip().lower()
    return not action.startswith("blocked_") and action not in _RULE_BLOCKED_ACTIONS


def _step4_candidate_meta(
    symbols_info: list,
    _step3_springboard_codes: list[str],
    step3_report_text: str = "",
) -> tuple[list[dict], int]:
    items: list[dict] = []
    seen_codes: set[str] = set()
    for item in symbols_info or []:
        code = str(item.get("code", "")).strip() if isinstance(item, dict) else ""
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        items.append(item)
    require_confirmed = env_bool("STEP4_REQUIRE_CONFIRMED_BUY_CANDIDATE", True)
    eligible = [item for item in items if _rule_eligible_step4_candidate(item)] if require_confirmed else items
    policy = step4_ai_candidate_policy()
    selected = eligible
    if policy == "veto_only":
        vetoed = set(extract_invalidated_codes(step3_report_text, [item.get("code", "") for item in eligible]))
        selected = [item for item in selected if str(item.get("code", "")).strip() not in vetoed]
    blocked = len(items) - len(eligible)
    return selected, blocked if require_confirmed else 0


def step4_candidate_meta(
    symbols_info: list,
    step3_springboard_codes: list[str],
    step3_report_text: str = "",
) -> tuple[list[dict], int]:
    """公开入口：给同层 runtime 复用 Step4 的规则准入（confirmed / VALIDATED）。

    影子账本要与实盘用同一套买许可，否则纸面成绩没有参考价值。跨模块 import 私有名会被
    tests/test_architecture_boundaries.py 拦下，因此在这里显式导出，而不是把逻辑复制一份。
    """
    return _step4_candidate_meta(symbols_info, step3_springboard_codes, step3_report_text)


def run_step4_pipeline(
    *,
    step4_target: dict,
    symbols_info: list,
    step3_springboard_codes: list[str],
    step3_report_text: str,
    benchmark_context: dict | None,
    api_key: str,
    model: str,
    provider: str,
    llm_base_url: str,
    logs_path: str | None,
    holdings_intraday_report: str = "",
) -> dict:
    t0 = datetime.now(TZ)
    tg_bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if not tg_bot_token or not tg_chat_id:
        log_line("Step4 私人再平衡: 跳过（TG 通知通道未配置）", logs_path)
        return _step4_summary(True, None, 0, "", "", "skipped (TG 通知通道未配置)")

    user_id = str(step4_target.get("user_id", "") or "").strip()
    portfolio_id = str(step4_target.get("portfolio_id", "") or "").strip()
    candidate_meta, blocked_by_rules = _step4_candidate_meta(
        symbols_info,
        step3_springboard_codes,
        step3_report_text,
    )
    blocked_msg = f"，规则拦截 {blocked_by_rules} 只" if blocked_by_rules else ""
    log_line(
        f"Step4 私人再平衡: AI候选策略={step4_ai_candidate_policy()}，规则准入 {len(candidate_meta)} 只{blocked_msg}",
        logs_path,
    )
    return _execute_step4_pipeline(
        user_id=user_id,
        portfolio_id=portfolio_id,
        candidate_meta=candidate_meta,
        step3_report_text=step3_report_text,
        benchmark_context=benchmark_context,
        api_key=api_key,
        model=model,
        provider=provider,
        llm_base_url=llm_base_url,
        tg_bot_token=tg_bot_token,
        tg_chat_id=tg_chat_id,
        logs_path=logs_path,
        started_at=t0,
        holdings_intraday_report=holdings_intraday_report,
    )


def _execute_step4_pipeline(
    *,
    user_id: str,
    portfolio_id: str,
    candidate_meta: list[dict],
    step3_report_text: str,
    benchmark_context: dict | None,
    api_key: str,
    model: str,
    provider: str,
    llm_base_url: str,
    tg_bot_token: str,
    tg_chat_id: str,
    logs_path: str | None,
    started_at: datetime,
    holdings_intraday_report: str,
) -> dict:
    try:
        ok, reason = run_step4(
            external_report=step3_report_text if candidate_meta else "",
            benchmark_context=benchmark_context,
            api_key=api_key,
            model=model,
            provider=provider,
            llm_base_url=llm_base_url,
            candidate_meta=candidate_meta,
            portfolio_id=portfolio_id,
            tg_bot_token=tg_bot_token,
            tg_chat_id=tg_chat_id,
            holdings_intraday_report=holdings_intraday_report,
        )
        err = None if ok else STEP4_REASON_MAP.get(reason, reason)
    except Exception as e:
        ok, reason, err = False, "unexpected_exception", str(e)
    elapsed = (datetime.now(TZ) - started_at).total_seconds()
    log_line(
        f"Step4 私人再平衡: user={user_id}, portfolio={portfolio_id}, ok={ok}, reason={reason}, err={err}", logs_path
    )
    return _step4_summary(ok and err is None, err, round(elapsed, 1), user_id, portfolio_id, reason)


def _step4_summary(ok: bool, err: str | None, elapsed_s: float, user_id: str, portfolio_id: str, reason: str) -> dict:
    return {
        "step": "私人再平衡",
        "ok": ok,
        "err": err,
        "elapsed_s": elapsed_s,
        "output": f"user={user_id}, portfolio={portfolio_id}, reason={reason}",
    }
