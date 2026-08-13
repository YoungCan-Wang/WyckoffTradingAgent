"""Step4 position and candidate context payload building."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from functools import partial

import pandas as pd

from core.hist_dates import latest_trade_date_from_hist
from core.holding_diagnostic import diagnose_one_stock, format_diagnostic_for_llm
from core.holding_time_policy import holding_time_action, is_mainline_track
from core.wyckoff_engine import FunnelConfig, normalize_hist_from_fetch
from integrations.fetch_a_share_csv import TradingWindow, fetch_hist
from tools.report_builder import generate_stock_payload
from tools.spot_patch import append_spot_bar_if_needed
from workflows.step4_models import CandidateMeta, PortfolioState, PositionItem, Step4PayloadContext
from workflows.step4_text import clean_text, normalize_stage, normalize_track

logger = logging.getLogger(__name__)

_EXTERNAL_REPORT_CODE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:SH|SZ|BJ)?\.?([0134568]\d{5})(?:\.(?:SH|SZ|BJ))?(?![A-Za-z0-9])"
)

_append_spot_bar_if_needed = partial(
    append_spot_bar_if_needed,
    env_prefix="STEP4",
    sleep_default=0.3,
    zero_fallback=True,
)


def prepare_step4_payload_context(
    portfolio: PortfolioState,
    window: TradingWindow,
    external_report: str,
    candidate_meta: list[dict] | None,
    *,
    atr_period: int,
    max_workers: int,
    enforce_target_trade_date: bool,
    max_external_report_candidates: int = 12,
) -> Step4PayloadContext:
    positions_payload, position_failures, live_value, latest_price_map, atr_map = format_position_payload(
        portfolio.positions,
        window,
        atr_period=atr_period,
        max_workers=max_workers,
        enforce_target_trade_date=enforce_target_trade_date,
    )
    total_equity = float(portfolio.free_cash + live_value)
    _log_total_equity_drift(portfolio, total_equity)
    candidate_codes, candidate_items, allowed_codes, candidate_meta_map, name_map = collect_step4_candidates(
        portfolio,
        candidate_meta,
        external_report,
        max_external_report_candidates=max_external_report_candidates,
    )
    candidate_payload, candidate_failures, candidate_latest_price_map, candidate_atr_map = format_candidate_payload(
        candidate_items,
        window,
        atr_period=atr_period,
        max_workers=max_workers,
        enforce_target_trade_date=enforce_target_trade_date,
    )
    truncation_note = _external_report_candidate_truncation_note(
        portfolio,
        candidate_meta,
        external_report,
        selected_count=len(candidate_codes),
        max_external_report_candidates=max_external_report_candidates,
    )
    if truncation_note:
        candidate_failures.append(truncation_note)
    latest_price_map.update(candidate_latest_price_map)
    atr_map.update(candidate_atr_map)
    return Step4PayloadContext(
        total_equity=total_equity,
        positions_payload=positions_payload,
        position_failures=position_failures,
        candidate_codes=candidate_codes,
        allowed_codes=allowed_codes,
        candidate_payload=candidate_payload,
        candidate_failures=candidate_failures,
        latest_price_map=latest_price_map,
        atr_map=atr_map,
        candidate_meta_map=candidate_meta_map,
        name_map=name_map,
    )


def collect_step4_candidates(
    portfolio: PortfolioState,
    candidate_meta: list[dict] | None,
    external_report: str,
    *,
    max_external_report_candidates: int = 12,
) -> tuple[list[str], list[dict], set[str], dict[str, CandidateMeta], dict[str, str]]:
    position_codes = [p.code for p in portfolio.positions]
    position_code_set = set(position_codes)
    candidate_codes: list[str] = []
    seen_candidate_codes: set[str] = set()
    candidate_items: list[dict] = []
    for item in candidate_meta or []:
        if not isinstance(item, dict):
            continue
        code = clean_text(item.get("code"))
        if not re.fullmatch(r"\d{6}", code) or code in position_code_set or code in seen_candidate_codes:
            continue
        seen_candidate_codes.add(code)
        candidate_codes.append(code)
        candidate_items.append(dict(item))
    if candidate_meta is None and max_external_report_candidates > 0:
        for code in extract_stock_codes(external_report):
            if code in position_code_set or code in seen_candidate_codes:
                continue
            seen_candidate_codes.add(code)
            candidate_codes.append(code)
            candidate_items.append(_external_report_candidate_item(code))
            if len(candidate_codes) >= max_external_report_candidates:
                break
    allowed_codes = set(position_codes + candidate_codes)
    candidate_meta_map = build_candidate_meta_map(
        candidate_meta if candidate_meta is not None else candidate_items, portfolio.positions
    )
    name_map = {p.code: p.name for p in portfolio.positions}
    for code, meta in candidate_meta_map.items():
        if code in allowed_codes and code not in name_map:
            name_map[code] = meta.name or code
    return candidate_codes, candidate_items, allowed_codes, candidate_meta_map, name_map


def _external_report_candidate_item(code: str) -> dict[str, str]:
    return {
        "code": code,
        "name": code,
        "tag": "外部报告候选",
        "source_type": "external_report",
    }


def _external_report_candidate_truncation_note(
    portfolio: PortfolioState,
    candidate_meta: list[dict] | None,
    external_report: str,
    *,
    selected_count: int,
    max_external_report_candidates: int,
) -> str:
    if candidate_meta is not None or max_external_report_candidates <= 0:
        return ""
    held_codes = {p.code for p in portfolio.positions}
    total_candidates = _external_report_candidate_count(external_report, held_codes)
    if total_candidates <= selected_count:
        return ""
    dropped = total_candidates - selected_count
    return (
        "external_report_candidates_truncated:"
        f" kept={selected_count}, dropped={dropped}, limit={max_external_report_candidates}"
    )


def _external_report_candidate_count(external_report: str, held_codes: set[str]) -> int:
    seen: set[str] = set()
    for code in extract_stock_codes(external_report):
        if code in held_codes or code in seen:
            continue
        seen.add(code)
    return len(seen)


def build_candidate_meta_map(
    candidate_meta: list[dict] | None,
    positions: list[PositionItem],
) -> dict[str, CandidateMeta]:
    meta_map: dict[str, CandidateMeta] = {}
    for item in candidate_meta or []:
        if not isinstance(item, dict):
            continue
        code = clean_text(item.get("code"))
        if not re.fullmatch(r"\d{6}", code):
            continue
        meta_map[code] = CandidateMeta(
            code=code,
            name=clean_text(item.get("name")) or code,
            tag=clean_text(item.get("tag")),
            track=normalize_track(item.get("track")),
            stage=normalize_stage(item.get("stage")),
            industry=clean_text(item.get("industry")),
            sector_state=clean_text(item.get("sector_state")),
            sector_state_code=clean_text(item.get("sector_state_code")),
            sector_note=clean_text(item.get("sector_note")),
            funnel_score=candidate_score(item),
            capital_migration_bonus=parse_float_like(item.get("capital_migration_bonus")),
            exit_signal=clean_text(item.get("exit_signal")),
            exit_price=parse_float_like(item.get("exit_price")),
            exit_reason=clean_text(item.get("exit_reason")),
            source_type=clean_text(item.get("source_type")) or "external",
            action_status=clean_text(item.get("action_status")),
            trade_readiness=clean_text(item.get("trade_readiness")),
            new_buy_allowed=parse_bool_like(item.get("new_buy_allowed")),
            label_ready=parse_bool_like(item.get("label_ready")),
            risk_factors=tuple(_candidate_risk_items(item)),
            next_step=clean_text(item.get("next_step")),
        )
    for pos in positions:
        existing = meta_map.get(pos.code)
        meta_map[pos.code] = CandidateMeta(
            code=pos.code,
            name=pos.name or pos.code,
            tag=(existing.tag if existing and existing.tag else "持仓"),
            track=(existing.track if existing and existing.track else None),
            stage=(existing.stage if existing and existing.stage else None),
            source_type="holding",
        )
    return meta_map


def fetch_latest_real_close(code: str, window: TradingWindow, *, enforce_target_trade_date: bool) -> float | None:
    for adjust, label in [("", "不复权"), ("qfq", "前复权")]:
        try:
            raw = fetch_hist(code, window, adjust)
            df = normalize_hist_from_fetch(raw).sort_values("date").reset_index(drop=True)
            if enforce_target_trade_date:
                df, patched = _append_spot_bar_if_needed(code, df, window.end_trade_date)
                if patched:
                    logger.info("%s 实时快照补偿成功（%s）", code, label)
                latest_trade_date = latest_trade_date_from_hist(df)
                if latest_trade_date != window.end_trade_date:
                    logger.info(
                        "%s %s交易日未对齐: latest_trade_date=%s, target_trade_date=%s",
                        code,
                        label,
                        latest_trade_date,
                        window.end_trade_date,
                    )
                    continue
            return float(df.iloc[-1]["close"])
        except Exception:
            logger.debug("%s fetch_latest_real_close failed (%s)", code, label, exc_info=True)
            continue
    return None


def load_qfq_history(code: str, window: TradingWindow, *, enforce_target_trade_date: bool) -> pd.DataFrame:
    raw_qfq = fetch_hist(code, window, "qfq")
    df_qfq = normalize_hist_from_fetch(raw_qfq).sort_values("date").reset_index(drop=True)
    if not enforce_target_trade_date:
        return df_qfq
    df_qfq, patched = _append_spot_bar_if_needed(code, df_qfq, window.end_trade_date)
    if patched:
        logger.info("%s 持仓数据已用实时快照补偿", code)
    latest_trade_date = latest_trade_date_from_hist(df_qfq)
    if latest_trade_date != window.end_trade_date:
        raise RuntimeError(f"qfq_latest_trade_date={latest_trade_date}, target_trade_date={window.end_trade_date}")
    return df_qfq


def calc_atr(df: pd.DataFrame, period: int) -> float | None:
    if df is None or df.empty:
        return None
    need_cols = {"high", "low", "close"}
    if not need_cols.issubset(set(df.columns)):
        return None
    data = df.copy().sort_values("date").reset_index(drop=True)
    high = pd.to_numeric(data["high"], errors="coerce")
    low = pd.to_numeric(data["low"], errors="coerce")
    close = pd.to_numeric(data["close"], errors="coerce")
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(max(int(period), 2)).mean()
    if atr.dropna().empty:
        return None
    return float(atr.iloc[-1])


def extract_stock_codes(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for code in _EXTERNAL_REPORT_CODE_RE.findall(text):
        if code in seen or not _is_supported_external_report_code(code):
            continue
        seen.add(code)
        out.append(code)
    return out


def _is_supported_external_report_code(code: str) -> bool:
    return bool(re.fullmatch(r"[0134568]\d{5}", code))


def parse_float_like(raw: object) -> float | None:
    try:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        return float(text)
    except Exception:
        logger.debug("parse_float_like failed for %s", raw, exc_info=True)
        return None


def parse_bool_like(raw: object) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if raw in (None, ""):
        return None
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return None


def candidate_score(item: dict) -> float | None:
    for key in ("priority_score", "score", "funnel_score"):
        score = parse_float_like(item.get(key))
        if score is not None:
            return score
    return None


def candidate_source(item: dict) -> str:
    return (
        clean_text(item.get("selection_source"))
        or clean_text(item.get("source_type"))
        or clean_text(item.get("candidate_lane"))
    )


def candidate_status(item: dict) -> str:
    return (
        clean_text(item.get("signal_status"))
        or clean_text(item.get("status"))
        or clean_text(item.get("candidate_status"))
    )


def candidate_context_line(item: dict) -> str:
    parts: list[str] = []
    score = candidate_score(item)
    if score is not None:
        parts.append(f"score={score:.2f}")
    layer3_score = parse_float_like(item.get("layer3_quality_score"))
    if layer3_score is not None and layer3_score > 0:
        parts.append(f"L3质量={layer3_score:.2f}")
    bonus = parse_float_like(item.get("capital_migration_bonus"))
    if bonus is not None and abs(bonus) > 1e-9:
        label = "资金迁移加分" if bonus > 0 else "资金迁移扣分"
        parts.append(f"{label}={bonus:+.2f}")
    source = candidate_source(item)
    if source:
        parts.append(f"来源={source}")
    lane = clean_text(item.get("candidate_lane")) or clean_text(item.get("entry_type"))
    if lane:
        parts.append(f"通道={lane}")
    parts.extend(candidate_quality_context_parts(item))
    return f"  [候选归因] {' | '.join(parts)}\n" if parts else ""


def candidate_quality_context_parts(item: dict) -> list[str]:
    parts = [
        _grade_score_context("候选影子", item.get("candidate_shadow_grade"), item.get("candidate_shadow_score")),
        _grade_score_context("入场", item.get("entry_quality_grade"), item.get("entry_quality_score")),
    ]
    if strategy := clean_text(item.get("selection_strategy")):
        parts.append(f"策略={strategy}")
    if item.get("is_ai_recommended") is True:
        parts.append("已AI推荐")
    if count := parse_float_like(item.get("recommend_count")):
        parts.append(f"推荐次数={count:.0f}")
    if status := clean_text(item.get("action_status")):
        parts.append(f"动作={status}")
    if readiness := clean_text(item.get("trade_readiness")):
        parts.append(f"交易就绪={readiness}")
    if item.get("new_buy_allowed") is False:
        parts.append("不允许新增买入")
    if item.get("label_ready") is False:
        parts.append("标签未成熟")
    if risk := _candidate_risk_context(item):
        parts.append(f"风险={risk}")
    if next_step := clean_text(item.get("next_step")):
        parts.append(f"下一步={next_step}")
    return [part for part in parts if part]


def _grade_score_context(label: str, grade: object, score: object) -> str:
    grade_text = clean_text(grade)
    score_value = parse_float_like(score)
    if grade_text and score_value is not None:
        return f"{label}={grade_text}/{score_value:.2f}"
    if grade_text:
        return f"{label}={grade_text}"
    if score_value is not None:
        return f"{label}={score_value:.2f}"
    return ""


def _candidate_risk_context(item: dict) -> str:
    return "；".join(_candidate_risk_items(item)[:3])


def _candidate_risk_items(item: dict) -> list[str]:
    risks = _text_items(item.get("risk_factors")) + _text_items(item.get("entry_quality_risk_flags"))
    return list(dict.fromkeys(risks))


def _text_items(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [text for item in value if (text := clean_text(item))]
    text = clean_text(value)
    return [text] if text else []


def prepend_candidate_context(payload: str, item: dict) -> str:
    line = candidate_context_line(item)
    if not line:
        return payload
    head, sep, tail = payload.partition("\n")
    return f"{head}\n{line}{tail}" if sep else f"{payload}\n{line.rstrip()}"


def format_position_payload(
    positions: list[PositionItem],
    window: TradingWindow,
    *,
    atr_period: int,
    max_workers: int,
    enforce_target_trade_date: bool,
) -> tuple[str, list[str], float, dict[str, float], dict[str, float]]:
    blocks: list[str] = []
    failures: list[str] = []
    raw_values: dict[str, float] = {}
    latest_close_map: dict[str, float] = {}
    atr_map: dict[str, float] = {}
    if not positions:
        return ("", [], 0.0, {}, {})

    from integrations.local_db import load_signals_by_codes

    signal_map = load_signals_by_codes([p.code for p in positions])
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_one_position,
                pos,
                window,
                signal_map.get(pos.code),
                atr_period=atr_period,
                enforce_target_trade_date=enforce_target_trade_date,
            ): pos
            for pos in positions
        }
        for future in as_completed(futures):
            pos = futures[future]
            try:
                meta_block, fail_msg, value, close, atr, _days = future.result()
            except Exception as e:
                failures.append(f"{pos.code} {pos.name}: 数据处理异常 {e}")
                logger.warning("持仓 %s 处理异常: %s", pos.code, e, exc_info=True)
                continue
            if fail_msg:
                failures.append(fail_msg)
            if meta_block:
                blocks.append(meta_block)
                raw_values[pos.code] = value
                latest_close_map[pos.code] = close
                if atr is not None:
                    atr_map[pos.code] = atr
    live_value_sum = _to_cny_total(raw_values, failures)
    return ("\n\n".join(blocks), failures, live_value_sum, latest_close_map, atr_map)


def _to_cny_total(raw_values: dict[str, float], failures: list[str]) -> float:
    """把各币种市值折算成人民币再加总。

    `total_equity` 是仓位上限、单票占比与 PROBE/ATTACK 预算的分母，混币加总会让分母
    虚高、所有仓位约束偏松。实测 1000 股 06881.HK @7.5 HKD 曾被当作 7500 CNY 计入，
    按 0.92 汇率高估约 600 元（8.7%）。

    汇率取不到时按 1.0 计并写入 failures——宁可报出来，也不要静默用错误汇率下单。
    """
    from core.portfolio_valuation import portfolio_currency
    from integrations.portfolio_market_value import load_cny_rates

    currencies = {portfolio_currency(code) for code in raw_values}
    if currencies <= {"CNY"}:
        return round(sum(raw_values.values()), 2)
    try:
        rates = load_cny_rates(currencies)
    except Exception as exc:
        failures.append(f"汇率获取失败，非人民币持仓按 1:1 计入总权益（分母可能偏高）: {exc}")
        logger.warning("[step4] 汇率获取失败: %s", exc)
        return round(sum(raw_values.values()), 2)
    total = 0.0
    for code, value in raw_values.items():
        currency = portfolio_currency(code)
        rate = float(rates.get(currency, 0.0) or 0.0)
        if rate <= 0:
            failures.append(f"{code} 缺少 {currency}->CNY 汇率，按 1:1 计入总权益（分母可能偏高）")
            rate = 1.0
        total += value * rate
    return round(total, 2)


def format_candidate_payload(
    candidate_items: list[dict],
    window: TradingWindow,
    *,
    atr_period: int,
    max_workers: int,
    enforce_target_trade_date: bool,
) -> tuple[str, list[str], dict[str, float], dict[str, float]]:
    if not candidate_items:
        return ("", [], {}, {})

    blocks_by_index: dict[int, str] = {}
    failures: list[str] = []
    latest_close_map: dict[str, float] = {}
    atr_map: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_one_candidate,
                item,
                window,
                atr_period=atr_period,
                enforce_target_trade_date=enforce_target_trade_date,
            ): (idx, item)
            for idx, item in enumerate(candidate_items)
        }
        for future in as_completed(futures):
            idx, item = futures[future]
            block, fail_msg, latest_close, atr14 = future.result()
            if fail_msg:
                failures.append(fail_msg)
            if block:
                blocks_by_index[idx] = block
            code = clean_text(item.get("code"))
            if latest_close is not None:
                latest_close_map[code] = latest_close
            if atr14 is not None:
                atr_map[code] = atr14
    ordered_blocks = [blocks_by_index[idx] for idx in sorted(blocks_by_index)]
    return ("\n\n".join(ordered_blocks), failures, latest_close_map, atr_map)


def _calc_holding_trade_days(df: pd.DataFrame, buy_dt: str, end_trade_date: date) -> int | None:
    if df is None or df.empty or "date" not in df.columns:
        return None
    if not str(buy_dt or "").strip():
        return None
    buy_ts = pd.to_datetime(buy_dt, errors="coerce")
    if pd.isna(buy_ts):
        return None
    buy_date = buy_ts.date()
    dates = pd.to_datetime(df["date"], errors="coerce").dropna().dt.date.tolist()
    dates = sorted({d for d in dates if d <= end_trade_date})
    if not dates:
        return None
    entry_trade_date = next((d for d in dates if d >= buy_date), None)
    if entry_trade_date is None:
        return None
    return int(sum(1 for d in dates if d >= entry_trade_date))


def _position_base_meta(
    pos: PositionItem,
    *,
    latest_close: float,
    atr14: float | None,
    hold_trade_days: int | None,
    signal_info: dict | None,
    atr_period: int,
    trade_date: str,
) -> str:
    pnl_pct = (latest_close - pos.cost) / pos.cost * 100.0 if pos.cost > 0 else 0.0
    stop_info = f"- 当前止损: {pos.stop_loss:.2f}\n" if pos.stop_loss is not None else "- 当前止损: 未设置\n"
    time_line = _holding_time_meta_line(hold_trade_days, signal_info)
    missing_buy = (
        "" if str(pos.buy_dt or "").strip() else "- 建仓日缺失: 结构退出不可用，不得用建仓前前高/箱体作为本仓硬止损\n"
    )
    return (
        f"### 持仓 {pos.code} {pos.name}\n"
        f"- 成本价: {pos.cost:.2f}\n"
        f"- 最新收盘(不复权优先): {latest_close:.2f}\n"
        f"- 浮盈亏: {pnl_pct:+.2f}%\n"
        f"{stop_info}"
        f"- ATR{atr_period}: {(f'{atr14:.3f}' if atr14 is not None else '-')}\n"
        f"- 持仓股数: {pos.shares}\n"
        f"{_sellable_meta_line(pos, trade_date)}"
        f"- 持仓交易日: {(hold_trade_days if hold_trade_days is not None else '-')}\n"
        f"{time_line}"
        f"- 买入日期: {pos.buy_dt or '-'}\n"
        f"{missing_buy}"
        f"- 信号类型: {signal_info.get('signal_type', '未记录') if signal_info else '未记录'}\n"
        f"- 信号状态: {signal_info.get('status', '未记录') if signal_info else '未记录'}\n"
        f"- 信号日期: {signal_info.get('signal_date', '-') if signal_info else '-'}\n"
    )


def _sellable_meta_line(pos: PositionItem, trade_date: str) -> str:
    if pos.sellable_shares(trade_date) >= max(int(pos.shares), 0):
        return ""
    return "- 可卖股数: 0（T+1，当日买入当日不可卖，EXIT/TRIM 会被 OMS 拒单）\n"


def _holding_time_meta_line(hold_trade_days: int | None, signal_info: dict | None) -> str:
    if hold_trade_days is None:
        return ""
    info = signal_info or {}
    mainline = is_mainline_track(
        info.get("wyckoff_track") or info.get("track"),
        info.get("wyckoff_stage") or info.get("stage"),
        info.get("signal_type") or info.get("wyckoff_tag"),
    )
    advice = holding_time_action(int(hold_trade_days), is_mainline=mainline)
    return f"- 时间管理: {advice.action} — {advice.reason}\n"


def _position_diagnostic_payload(pos: PositionItem, df_qfq: pd.DataFrame) -> tuple[str, str]:
    try:
        diag = diagnose_one_stock(
            code=pos.code,
            name=pos.name,
            cost=pos.cost,
            df=df_qfq,
            bench_df=None,
            cfg=FunnelConfig(),
            buy_dt=pos.buy_dt,
        )
        payload = generate_stock_payload(
            stock_code=pos.code,
            stock_name=pos.name,
            wyckoff_tag="持仓",
            df=df_qfq,
            track=diag.track if diag.track != "Unknown" else None,
            stage=diag.accum_stage,
            exit_signal=diag.exit_signal,
            exit_price=diag.exit_price,
            exit_reason=diag.exit_reason,
        )
        return f"- {format_diagnostic_for_llm(diag)}\n", payload
    except Exception:
        logger.debug("%s diagnostic formatting failed, using fallback", pos.code, exc_info=True)
        payload = generate_stock_payload(stock_code=pos.code, stock_name=pos.name, wyckoff_tag="持仓", df=df_qfq)
        return "", payload


def _position_snapshot_fallback(
    pos: PositionItem,
    window: TradingWindow,
    error: Exception,
    *,
    enforce_target_trade_date: bool,
) -> tuple[str, str, float, float, float | None, int | None]:
    latest_close = fetch_latest_real_close(pos.code, window, enforce_target_trade_date=enforce_target_trade_date)
    if latest_close is None:
        return ("", f"{pos.code}:{error}", 0.0, 0.0, None, None)
    live_val = latest_close * max(pos.shares, 0)
    fallback_meta = (
        f"### 持仓 {pos.code} {pos.name}\n"
        f"- 成本价: {pos.cost:.2f}\n"
        f"- 最新收盘(快照补偿): {latest_close:.2f}\n"
        f"- 持仓股数: {pos.shares}\n"
        "- 数据状态: 日线未齐，已降级为快照风控。\n"
    )
    return (fallback_meta, f"{pos.code}:{error}", live_val, latest_close, None, None)


def _process_one_position(
    pos: PositionItem,
    window: TradingWindow,
    signal_info: dict | None = None,
    *,
    atr_period: int,
    enforce_target_trade_date: bool,
) -> tuple[str, str, float, float, float | None, int | None]:
    try:
        df_qfq = load_qfq_history(pos.code, window, enforce_target_trade_date=enforce_target_trade_date)
        atr14 = calc_atr(df_qfq, atr_period)
        latest_close = fetch_latest_real_close(pos.code, window, enforce_target_trade_date=enforce_target_trade_date)
        failure_msg = "" if latest_close is not None else f"{pos.code}:real_close_fallback_to_qfq"
        latest_close = latest_close if latest_close is not None else float(df_qfq.iloc[-1]["close"])
        hold_days = _calc_holding_trade_days(df_qfq, pos.buy_dt, window.end_trade_date)
        meta = _position_base_meta(
            pos,
            latest_close=latest_close,
            atr14=atr14,
            hold_trade_days=hold_days,
            signal_info=signal_info,
            atr_period=atr_period,
            trade_date=window.end_trade_date.isoformat(),
        )
        diag_text, payload = _position_diagnostic_payload(pos, df_qfq)
        live_val = latest_close * max(pos.shares, 0)
        return (meta + diag_text + "\n" + payload, failure_msg, live_val, latest_close, atr14, hold_days)
    except Exception as e:
        return _position_snapshot_fallback(
            pos,
            window,
            e,
            enforce_target_trade_date=enforce_target_trade_date,
        )


def _process_one_candidate(
    item: dict,
    window: TradingWindow,
    *,
    atr_period: int,
    enforce_target_trade_date: bool,
) -> tuple[str, str, float | None, float | None]:
    code = clean_text(item.get("code"))
    name = clean_text(item.get("name")) or code
    try:
        df_qfq = load_qfq_history(code, window, enforce_target_trade_date=enforce_target_trade_date)
        atr14 = calc_atr(df_qfq, atr_period)
        latest_close = fetch_latest_real_close(code, window, enforce_target_trade_date=enforce_target_trade_date)
        if latest_close is None:
            latest_close = float(df_qfq.iloc[-1]["close"])
        payload = generate_stock_payload(
            stock_code=code,
            stock_name=name,
            wyckoff_tag=clean_text(item.get("tag")) or "漏斗候选",
            df=df_qfq,
            industry=clean_text(item.get("industry")) or None,
            track=clean_text(item.get("track")) or None,
            stage=clean_text(item.get("stage")) or None,
            funnel_score=candidate_score(item),
            sector_state=clean_text(item.get("sector_state")) or None,
            sector_state_code=clean_text(item.get("sector_state_code")) or None,
            sector_note=clean_text(item.get("sector_note")) or None,
            exit_signal=clean_text(item.get("exit_signal")) or None,
            exit_price=parse_float_like(item.get("exit_price")),
            exit_reason=clean_text(item.get("exit_reason")) or None,
            springboard_grade=clean_text(item.get("springboard_grade")) or None,
            candidate_source=candidate_source(item) or None,
            signal_status=candidate_status(item) or None,
            confirm_date=clean_text(item.get("confirm_date")) or None,
            confirm_reason=clean_text(item.get("confirm_reason")) or None,
        )
        return (prepend_candidate_context(payload, item), "", latest_close, atr14)
    except Exception as e:
        return ("", f"{code}:{e}", None, None)


def _log_total_equity_drift(portfolio: PortfolioState, computed_total: float) -> None:
    if portfolio.total_equity is None:
        return
    drift = abs(float(portfolio.total_equity) - computed_total)
    if drift < 1e-6:
        return
    logger.info(
        "total_equity 已按实时口径重算: input=%.2f, computed=%.2f, drift=%.2f",
        float(portfolio.total_equity),
        computed_total,
        drift,
    )
