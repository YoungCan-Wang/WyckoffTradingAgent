"""Candidate loading workflow for the tail-buy intraday job."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from core.candidate_report_semantics import candidate_reason_payload, optional_candidate_score
from core.constants import TABLE_RECOMMENDATION_TRACKING, TABLE_SIGNAL_PENDING
from core.holding_time_policy import is_mainline_track
from core.tail_buy.strategy import TailBuyCandidate, pick_tail_candidates
from integrations.fetch_a_share_csv import resolve_trading_window
from integrations.supabase_base import create_admin_client, is_admin_configured
from integrations.supabase_portfolio import load_portfolio_state
from utils.env import env_float as _env_float
from utils.env import env_int as _env_int
from utils.safe import safe_float
from workflows.tail_buy_utils import current_time, log_line, normalize_code6


def _candidate_priority_key(item: TailBuyCandidate) -> tuple:
    """confirmed 优先，主线/趋势次之，再按信号分。"""
    mainline = is_mainline_track(
        item.candidate_lane or item.signal_type,
        item.entry_type,
        item.candidate_status or item.signal_key,
    )
    return (item.status != "confirmed", 0 if mainline else 1, -float(item.signal_score or 0.0), item.code)


def resolve_tail_buy_trade_dates(logs_path: str | None = None) -> tuple[str, str]:
    calendar_today = current_time().date()
    try:
        return _resolve_calendar_trade_dates(calendar_today)
    except Exception as exc:
        prev_trade = (calendar_today - timedelta(days=1)).isoformat()
        today_trade = calendar_today.isoformat()
        log_line(
            f"交易日历解析失败，降级为自然日: prev={prev_trade}, today={today_trade}, err={exc}",
            logs_path,
        )
        return prev_trade, today_trade


def load_tail_candidates(
    target_signal_date: str,
    portfolio_id: str,
    logs_path: str | None = None,
    *,
    lookback_days: int = 15,
    strict_signal_date: bool = False,
    include_holdings: bool = True,
) -> tuple[list[TailBuyCandidate], str]:
    pending = _load_signal_pending_candidates(
        target_signal_date,
        logs_path,
        lookback_days=lookback_days,
        strict_signal_date=strict_signal_date,
    )
    holdings = _load_holding_candidates(portfolio_id, target_signal_date, logs_path) if include_holdings else []
    pending_codes = {candidate.code for candidate in pending}
    holding_supplement = [candidate for candidate in holdings if candidate.code not in pending_codes]
    occupied_codes = pending_codes | {candidate.code for candidate in holding_supplement}
    review_supplement = []
    if not strict_signal_date:
        reviews = _load_recommendation_review_candidates(target_signal_date, logs_path)
        review_supplement = [candidate for candidate in reviews if candidate.code not in occupied_codes]

    merged = pending + holding_supplement + review_supplement
    merged.sort(key=_candidate_priority_key)
    source_name = "signal_pending_exact" if strict_signal_date else "signal_pending_15d"
    holding_text = f" + holding={len(holding_supplement)}" if include_holdings else ""
    review_text = f" + rec_review={len(review_supplement)}" if review_supplement else ""
    source_desc = (
        f"{source_name}={len(pending)}{holding_text}{review_text} "
        f"(target={target_signal_date}, portfolio={portfolio_id})"
    )
    log_line(
        f"候选池加载完成: signal_pending={len(pending)}, 持仓补充={len(holding_supplement)}, "
        f"推荐复核={len(review_supplement)}, 合计={len(merged)}",
        logs_path,
    )
    return merged, source_desc


def _resolve_calendar_trade_dates(calendar_today: date) -> tuple[str, str]:
    window = resolve_trading_window(end_calendar_day=calendar_today, trading_days=2)
    prev_trade = window.start_trade_date.isoformat()
    today_trade = window.end_trade_date.isoformat()
    if window.end_trade_date < calendar_today:
        prev_trade = today_trade
    return prev_trade, today_trade


def _load_signal_pending_candidates(
    target_signal_date: str,
    logs_path: str | None = None,
    lookback_days: int = 15,
    strict_signal_date: bool = False,
) -> list[TailBuyCandidate]:
    if not is_admin_configured():
        raise RuntimeError("Supabase 凭据未配置，无法读取 signal_pending")

    cutoff_date = _signal_cutoff_date(target_signal_date, lookback_days)
    rows = _fetch_signal_pending_rows(cutoff_date, exact_date=target_signal_date if strict_signal_date else None)
    picked = pick_tail_candidates(rows, cutoff_date=cutoff_date)
    log_line(
        f"signal_pending 候选加载: raw={len(rows)}, picked={len(picked)}, "
        f"cutoff={cutoff_date}, target={target_signal_date}, strict={strict_signal_date}",
        logs_path,
    )
    return picked


def _load_holding_candidates(
    portfolio_id: str,
    target_signal_date: str,
    logs_path: str | None = None,
) -> list[TailBuyCandidate]:
    state = load_portfolio_state(portfolio_id)
    positions = state.get("positions", []) if isinstance(state, dict) else []
    candidates = [_holding_candidate(pos, target_signal_date) for pos in positions]
    candidates = [candidate for candidate in candidates if candidate is not None]
    log_line(f"持仓候选加载: portfolio={portfolio_id}, count={len(candidates)}", logs_path)
    return candidates


def _signal_cutoff_date(target_signal_date: str, lookback_days: int) -> str:
    lookback = int(lookback_days * 1.5)
    return (datetime.strptime(target_signal_date, "%Y-%m-%d") - timedelta(days=lookback)).strftime("%Y-%m-%d")


def has_signal_pending_on_date(target_signal_date: str, logs_path: str | None = None) -> bool:
    if not is_admin_configured():
        log_line("Supabase 凭据未配置，无法检查今日 signal_pending", logs_path)
        return False
    try:
        rows = (
            create_admin_client()
            .table(TABLE_SIGNAL_PENDING)
            .select("id")
            .in_("status", ["pending", "confirmed"])
            .eq("signal_date", target_signal_date)
            .limit(1)
            .execute()
            .data
            or []
        )
        return bool(rows)
    except Exception as exc:
        log_line(f"检查 signal_pending({target_signal_date}) 失败: {exc}", logs_path)
        return False


def _fetch_signal_pending_rows(cutoff_date: str, *, exact_date: str | None = None) -> list[dict]:
    client = create_admin_client()
    try:
        query = (
            client.table(TABLE_SIGNAL_PENDING)
            .select(
                "code,name,signal_type,signal_score,status,signal_date,regime,snap_support,snap_ma20,"
                "snap_close,snap_ma50,strategy_version,candidate_lane,entry_type,signal_key,candidate_status,"
                "candidate_reasons,candidate_theme,candidate_phase,candidate_role,"
                "mainline_score,theme_score,stock_role_score,quality_score,timing_score"
            )
            .in_("status", ["pending", "confirmed"])
        )
        query = query.eq("signal_date", exact_date) if exact_date else query.gte("signal_date", cutoff_date)
        return query.order("signal_date", desc=True).limit(8000).execute().data or []
    except Exception as exc:
        raise RuntimeError(f"读取 signal_pending 失败: {exc}") from exc


def _load_recommendation_review_candidates(
    target_signal_date: str,
    logs_path: str | None = None,
) -> list[TailBuyCandidate]:
    cutoff = _recommendation_cutoff_int(target_signal_date)
    rows = _fetch_recommendation_review_rows(cutoff)
    candidates = _recommendation_review_candidates(rows, target_signal_date)
    log_line(
        f"推荐表复核候选加载: raw={len(rows)}, picked={len(candidates)}, cutoff={cutoff}",
        logs_path,
    )
    return candidates


def _recommendation_cutoff_int(target_signal_date: str) -> int:
    days = _env_int("TAIL_BUY_RECOMMENDATION_LOOKBACK_DAYS", 90)
    cutoff = datetime.strptime(target_signal_date, "%Y-%m-%d") - timedelta(days=max(days, 1))
    return int(cutoff.strftime("%Y%m%d"))


def _fetch_recommendation_review_rows(cutoff_recommend_date: int) -> list[dict]:
    try:
        return (
            create_admin_client()
            .table(TABLE_RECOMMENDATION_TRACKING)
            .select(
                "code,name,recommend_date,initial_price,current_price,change_pct,funnel_score,"
                "recommend_count,is_ai_recommended,rag_vetoed,candidate_lane,entry_type,signal_key,"
                "candidate_status,candidate_reasons,candidate_theme,candidate_phase,candidate_role,"
                "mainline_score,theme_score,stock_role_score,quality_score,timing_score,"
                "mfe_pct,mae_pct"
            )
            .gte("recommend_date", cutoff_recommend_date)
            .order("recommend_date", desc=True)
            .limit(3000)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        raise RuntimeError(f"读取 recommendation_tracking 复核候选失败: {exc}") from exc


def _recommendation_review_candidates(rows: list[dict], target_signal_date: str) -> list[TailBuyCandidate]:
    oversold = _pick_review_bucket(rows, kind="oversold", threshold=_env_float("TAIL_BUY_REVIEW_OVERSOLD_PCT", -30.0))
    momentum = _pick_review_bucket(rows, kind="momentum", threshold=_env_float("TAIL_BUY_REVIEW_MOMENTUM_PCT", 40.0))
    return [
        *_map_recommendation_rows(oversold, target_signal_date, "rec_deep_pullback"),
        *_map_recommendation_rows(momentum, target_signal_date, "rec_momentum_continuation"),
    ]


def _pick_review_bucket(rows: list[dict], *, kind: str, threshold: float) -> list[dict]:
    filtered = [row for row in rows if _is_review_bucket_match(row, kind, threshold)]
    by_code = _dedupe_review_rows(filtered, kind)
    limit = _env_int("TAIL_BUY_REVIEW_MAX_PER_BUCKET", 20)
    reverse = kind == "momentum"
    return sorted(by_code.values(), key=lambda row: safe_float(row.get("change_pct")), reverse=reverse)[: max(limit, 0)]


def _is_review_bucket_match(row: dict, kind: str, threshold: float) -> bool:
    if _truthy(row.get("rag_vetoed")) or safe_float(row.get("current_price")) <= 0:
        return False
    change = safe_float(row.get("change_pct"))
    return change >= threshold if kind == "momentum" else change <= threshold


def _dedupe_review_rows(rows: list[dict], kind: str) -> dict[str, dict]:
    by_code: dict[str, dict] = {}
    for row in rows:
        code = normalize_code6(row.get("code"))
        if not code:
            continue
        old = by_code.get(code)
        if old is None or _prefer_review_row(row, old, kind):
            by_code[code] = row
    return by_code


def _prefer_review_row(new: dict, old: dict, kind: str) -> bool:
    new_change, old_change = safe_float(new.get("change_pct")), safe_float(old.get("change_pct"))
    if kind == "momentum" and new_change != old_change:
        return new_change > old_change
    if kind != "momentum" and new_change != old_change:
        return new_change < old_change
    return _int(new.get("recommend_date")) > _int(old.get("recommend_date"))


def _map_recommendation_rows(rows: list[dict], target_signal_date: str, signal_type: str) -> list[TailBuyCandidate]:
    return [_recommendation_candidate(row, target_signal_date, signal_type) for row in rows]


def _recommendation_candidate(row: dict, target_signal_date: str, signal_type: str) -> TailBuyCandidate:
    change_pct = safe_float(row.get("change_pct"))
    current_price = safe_float(row.get("current_price"))
    return TailBuyCandidate(
        code=normalize_code6(row.get("code")),
        name=str(row.get("name", "") or row.get("code") or "").strip(),
        signal_date=target_signal_date,
        status="confirmed",
        signal_type=signal_type,
        signal_score=_recommendation_signal_score(change_pct, signal_type),
        candidate_lane="recommendation_review",
        entry_type="deep_pullback" if signal_type == "rec_deep_pullback" else "momentum_continuation",
        signal_key=signal_type,
        candidate_status="推荐后深跌复核" if signal_type == "rec_deep_pullback" else "推荐后强趋势延续",
        candidate_reasons=candidate_reason_payload(row.get("candidate_reasons")),
        candidate_theme=str(row.get("candidate_theme", "") or "").strip(),
        candidate_phase=str(row.get("candidate_phase", "") or "").strip(),
        candidate_role=str(row.get("candidate_role", "") or "").strip(),
        mainline_score=optional_candidate_score(row.get("mainline_score")),
        theme_score=optional_candidate_score(row.get("theme_score")),
        stock_role_score=optional_candidate_score(row.get("stock_role_score")),
        snap={
            "snap_support": current_price,
            "snap_close": current_price,
            "snap_ma20": 0.0,
            "snap_recommend_date": row.get("recommend_date"),
            "snap_change_pct": change_pct,
        },
    )


def _recommendation_signal_score(change_pct: float, signal_type: str) -> float:
    if signal_type == "rec_deep_pullback":
        return min(95.0, 60.0 + max(abs(change_pct) - 30.0, 0.0) * 1.5)
    return min(95.0, 60.0 + max(change_pct - 40.0, 0.0) * 0.8)


def _int(value: object, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def _holding_candidate(pos: dict, target_signal_date: str) -> TailBuyCandidate | None:
    code = normalize_code6(pos.get("code"))
    if not code:
        return None
    return TailBuyCandidate(
        code=code,
        name=str(pos.get("name", "") or code).strip() or code,
        signal_date=target_signal_date,
        status="confirmed",
        signal_type="holding",
        signal_score=0.0,
    )
