"""信号确认逻辑：pending → survived / confirmed / expired。纯业务，不依赖 DB。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

SIGNAL_TTL_DAYS: dict[str, int] = {
    "sos": 2,
    "spring": 3,
    "lps": 3,
    "evr": 2,
    "compression": 3,
    # 趋势/主线缩短确认窗口，避免 3–5 日 alpha 被确认链吃掉。
    "trend_pullback": 2,
    "trend_breakout": 2,
    "trend_lane_pullback": 2,
    "main_force_entry": 2,
    "sector_strength": 2,
    "wyckoff_structure": 2,
    "mainline": 2,
}
TREND_CONFIRM_SIGNALS = {
    "trend_pullback",
    "trend_breakout",
    "trend_lane_pullback",
    "sector_strength",
    "wyckoff_structure",
    "mainline",
    "main_force_entry",
}


def check_confirmation(
    signal_type: str,
    snap: dict[str, Any],
    today_ohlcv: dict[str, float],
    days_elapsed: int,
) -> tuple[str, str]:
    """返回 (new_status, reason)，区分“未失效”和“正向确认”。"""
    ttl = SIGNAL_TTL_DAYS.get(signal_type, 3)
    if days_elapsed >= ttl:
        return "expired", f"TTL {ttl}天已到，未满足确认条件"
    fn = _CONFIRM_DISPATCH.get(signal_type)
    if fn is None:
        return "expired", f"未知信号类型: {signal_type}"
    status, reason = fn(snap, today_ohlcv, days_elapsed)
    if status == "pending" and days_elapsed > 0:
        return "survived", reason
    return status, reason


def _close_position(today: dict[str, float], reference_close: float = 0.0) -> float:
    high = float(today.get("high", 0) or 0)
    low = float(today.get("low", 0) or 0)
    close = float(today.get("close", 0) or 0)
    if high > low:
        return (close - low) / (high - low)
    if reference_close > 0 and close > reference_close:
        return 1.0
    if reference_close > 0 and close < reference_close:
        return 0.0
    return 0.5


def _confirm_sos(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    # 收紧依据：仅"不跌破+缩量"就放行过于宽松，补充确认日站稳 MA20 附近，验证点火后
    # 确有资金承接而非一日游脉冲。
    #
    # 注：这里原先写着"点火类胜率仅 10-20%，回踩/LPS 44-47%"。2026-09 用 T+1 复权开盘
    # 买入、第 h 日复权收盘卖出、扣双边 0.15% 后为正算胜率复算(窗口 05-25~09-08)，
    # 这个 2~4 倍的差距不存在，点火在短持有期反而略高：
    #   h=1  sos 44.9 (n=661) / trend_pullback 40.1 (n=374) / lps 37.8 (n=394)
    #   h=5  sos 38.9 (n=620) / trend_pullback 36.1 (n=338) / lps 35.8 (n=360)
    # 原数字出自 signal_outcomes.return_pct，那一列从信号日收盘起算——点火当天大涨、
    # 次日跳空，收盘价买不到，这个偏差对点火类系统性更重，两族不可比。
    # 下面的 MA20 要求不依赖那组数字，保留。
    snap_low, snap_close, snap_vol = snap.get("snap_low", 0), snap.get("snap_close", 0), snap.get("snap_volume", 0)
    ma20 = today.get("ma20", 0)
    if today["low"] < snap_low:
        return "expired", f"跌破信号日低点 {snap_low:.2f}"
    if snap_vol > 0 and today["volume"] > snap_vol * 0.8 and today["close"] < snap_close * 0.97:
        return "expired", "放量回落，非缩量确认"
    holds_ma20 = ma20 <= 0 or today["close"] >= ma20 * 0.99
    if (
        snap_vol > 0
        and today["volume"] < snap_vol * 0.8
        and today["low"] >= snap_low
        and today["close"] >= snap_close
        and holds_ma20
    ):
        return "confirmed", f"缩量确认，收盘 {today['close']:.2f} 守住信号日收盘且站稳MA20"
    return "pending", "等待缩量+站稳MA20确认"


def _confirm_spring(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    support, snap_ma20 = snap.get("snap_support", 0), snap.get("snap_ma20", 0)
    if today["low"] < support * 0.98:
        return "expired", f"跌破支撑 {support:.2f}"
    if today["close"] > support and today["close"] >= snap_ma20 * 0.97:
        return "confirmed", f"守住支撑 {support:.2f}，收盘接近 MA20"
    return "pending", "等待收回 MA20"


def _confirm_lps(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    snap_ma20 = float(snap.get("snap_ma20", 0) or 0)
    snap_close = float(snap.get("snap_close", 0) or 0)
    snap_vol = float(snap.get("snap_volume", 0) or 0)
    today_ma20 = float(today.get("ma20", 0) or 0)
    if today["low"] < snap_ma20 * 0.98:
        return "expired", f"跌破 MA20 {snap_ma20:.2f}"
    if snap_vol > 0 and today["volume"] > snap_vol * 1.5:
        return "expired", "异常放量，LPS 逻辑失效"
    holds_support = today["close"] >= max(snap_ma20, today_ma20 * 0.995)
    holds_signal_close = snap_close <= 0 or today["close"] >= snap_close * 0.995
    dry = snap_vol <= 0 or today["volume"] <= snap_vol * 0.90
    bullish = today["close"] >= today.get("open", today["close"])
    strong_close = _close_position(today, snap_close) >= 0.60
    if holds_support and holds_signal_close and strong_close and (dry or bullish):
        return "confirmed", f"需求确认：高收守住信号区与当日MA20，收盘 {today['close']:.2f}"
    if holds_support and (snap_vol <= 0 or today["volume"] <= snap_vol * 1.2):
        return "pending", "结构未失效但缺少高收/需求证据"
    return "pending", "等待守住当日MA20并出现高收需求确认"


def _confirm_evr(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    # 同 _confirm_sos：单纯"收盘不跌破事件低点"不足以过滤滞涨假企稳，补充站稳 MA20。
    #
    # 注：这里原先写着"EVR(二次确认) 实盘胜率仅 9.7%，比未确认样本更差"。两点都复现不了。
    # 一是量级：同口径(T+1 复权开盘进、第 h 日复权收盘出、扣双边 0.15%)下 EVR 是
    # h=1 46.8 / h=3 40.3 / h=5 47.4 / h=10 36.4 (n=154)，和 sos/回踩/LPS 完全重叠，
    # 没有一个 horizon 接近 9.7。二是"二次确认"这个子集不存在：signal_pending 全表
    # 642 行里 evr 是 0 行，EVR 从未进过确认池，也就没有确认/未确认可比。
    event_low, snap_close = snap.get("snap_support", 0), snap.get("snap_close", 0)
    ma20 = today.get("ma20", 0)
    if today["close"] < event_low:
        return "expired", f"跌破事件日低点 {event_low:.2f}"
    holds_ma20 = ma20 <= 0 or today["close"] >= ma20 * 0.99
    if today["close"] >= event_low and today["close"] >= snap_close * 0.98 and holds_ma20:
        return "confirmed", f"守住 {event_low:.2f} 且站稳MA20，收盘 {today['close']:.2f}"
    return "pending", "等待企稳+站稳MA20确认"


def _confirm_compression(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    support = snap.get("snap_support", 0)
    snap_close = snap.get("snap_close", 0)
    snap_vol = snap.get("snap_volume", 0)
    if today["low"] < support * 0.97:
        return "expired", f"跌破压缩区间下沿 {support:.2f}"
    if snap_vol > 0 and today["volume"] > snap_vol * 2.0:
        if today["close"] > snap_close * 1.01:
            return "confirmed", f"放量向上突破压缩区间，收盘 {today['close']:.2f}"
        return "expired", "放量下破，压缩结构失效"
    if snap_vol > 0 and today["volume"] <= snap_vol * 1.0 and today["close"] >= support:
        return "confirmed", f"维持缩量窄幅，守住 {support:.2f}"
    return "pending", "等待继续缩量确认"


def _confirm_trend_candidate(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    support = snap.get("snap_support", 0) or snap.get("snap_ma20", 0) or snap.get("snap_low", 0)
    snap_close = snap.get("snap_close", 0)
    snap_vol = snap.get("snap_volume", 0)
    snap_low = snap.get("snap_low", 0) or support
    ma20 = today.get("ma20", 0) or snap.get("snap_ma20", 0)
    if support > 0 and today["low"] < support * 0.985:
        return "expired", f"跌破确认支撑 {support:.2f}"
    if snap_vol > 0 and today["volume"] > snap_vol * 1.8 and today["close"] < max(support, snap_close) * 0.985:
        return "expired", "放量跌破确认区，趋势候选失效"
    # 0–1 日快速确认：守住信号低点 +（缩量或阳线），不必死等 MA20。
    if days_elapsed <= 1 and snap_low > 0 and today["close"] >= snap_low and today["close"] >= snap_close * 0.985:
        dry = snap_vol <= 0 or today["volume"] <= snap_vol * 1.05
        bullish = today["close"] >= today.get("open", today["close"])
        if dry or bullish:
            return "confirmed", f"快速确认：守住信号区，收盘 {today['close']:.2f}"
    if ma20 > 0 and today["close"] >= ma20 * 0.995 and today["close"] >= snap_close * 0.985:
        return "confirmed", f"守住MA20/信号收盘，收盘 {today['close']:.2f}"
    if support > 0 and today["close"] >= support and today["close"] >= snap_close:
        return "confirmed", f"守住支撑并收回信号收盘 {snap_close:.2f}"
    return "pending", "等待主线/趋势买点确认"


_CONFIRM_DISPATCH = {
    "sos": _confirm_sos,
    "spring": _confirm_spring,
    "lps": _confirm_lps,
    "evr": _confirm_evr,
    "compression": _confirm_compression,
    **{signal: _confirm_trend_candidate for signal in TREND_CONFIRM_SIGNALS},
}


# 走 compute_support_level 的 default 分支（近 20 日最低价）——起跳板 C 需要真支撑口径。
_SPRINGBOARD_SUPPORT_SIGNAL = "__springboard_support__"


def compute_support_level(
    df: pd.DataFrame,
    signal_type: str,
    window: int = 60,
) -> float:
    """根据信号类型计算支撑位，可作为候选股的参考止损位。"""
    df_s = df.sort_values("date") if "date" in df.columns else df
    last = df_s.iloc[-1]
    if signal_type in ("spring", "compression"):
        zone = df_s.iloc[-(window + 2) : -2] if len(df_s) > window + 2 else df_s.iloc[:-2]
        return float(zone["close"].min()) if len(zone) > 0 else float(last["low"])
    if signal_type == "sos":
        return float(df_s["high"].tail(21).iloc[:-1].max()) if len(df_s) >= 21 else float(last["high"])
    if signal_type == "lps":
        return float(df_s["close"].rolling(20).mean().iloc[-1]) if len(df_s) >= 20 else float(last["close"])
    return float(df_s["low"].tail(20).min())


def _springboard_date(df_s: pd.DataFrame, idx: int) -> str:
    if "date" not in df_s.columns:
        return str(idx)
    parsed = pd.to_datetime(df_s.iloc[idx].get("date"), errors="coerce")
    return "" if pd.isna(parsed) else parsed.date().isoformat()


def _metric(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return round(value, 4) if pd.notna(value) else None


def _springboard_evidence(
    df_s: pd.DataFrame,
    vol_ratio: pd.Series,
    close_pos: pd.Series,
    low: pd.Series,
    support: float,
    tolerance: float,
    window: int,
) -> tuple[int, dict[str, Any]]:
    tail_idx = list(df_s.tail(5).index)
    a_hits = []
    for i in tail_idx:
        vr = _metric(vol_ratio.loc[i])
        cp = _metric(close_pos.loc[i])
        if vr is not None and cp is not None and vr < 0.8 and cp > 60:
            a_hits.append({"date": _springboard_date(df_s, int(i)), "vol_ratio": vr, "close_pos": cp})
    touch_idx = []
    for i in df_s.tail(window).index:
        low_value = _metric(low.loc[i])
        if low_value is not None and abs(low_value - support) <= tolerance:
            touch_idx.append(int(i))
    last_idx = int(df_s.index[-1])
    evidence = {
        "a_hits": a_hits,
        "b_last": {
            "date": _springboard_date(df_s, last_idx),
            "vol_ratio": _metric(vol_ratio.loc[last_idx]),
            "close_pos": _metric(close_pos.loc[last_idx]),
        },
        "c_support": {
            "support": _metric(support),
            "tolerance": _metric(tolerance),
            "touch_dates": [_springboard_date(df_s, i) for i in touch_idx[-8:]],
        },
    }
    return len(touch_idx), evidence


def _springboard_result(
    a: bool,
    b: bool,
    c: bool,
    support: float,
    touches: int,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    parts = [name for name, ok in (("A", a), ("B", b), ("C", c)) if ok]
    return {
        "a": a,
        "b": b,
        "c": c,
        "grade": "+".join(parts) if parts else "none",
        "met_count": len(parts),
        "support": _metric(support),
        "touch_count": touches,
        "evidence": evidence,
    }


def score_springboard_abc(
    df: pd.DataFrame,
    signal_type: str,
    window: int = 60,
) -> dict[str, Any]:
    """量化计算起跳板 A/B/C 三个硬门槛。

    A: 近5日有缩量测试（vol_ratio < 0.8 且 close_pos > 60%）
    B: 最后一根K线放量突破（vol_ratio >= 1.5 且 close_pos > 70%）
    C: 支撑位在 window 内被 low 触碰 >= 2 次（tolerance 5%）
    """
    df_s = (df.sort_values("date") if "date" in df.columns else df).reset_index(drop=True)
    close = pd.to_numeric(df_s["close"], errors="coerce")
    high = pd.to_numeric(df_s["high"], errors="coerce")
    low = pd.to_numeric(df_s["low"], errors="coerce")
    volume = pd.to_numeric(df_s["volume"], errors="coerce")
    vol_ma20 = volume.rolling(20).mean()
    vol_ratio = volume / vol_ma20.where(vol_ma20 != 0)
    span = high - low
    span = span.where(span != 0)
    close_pos = ((close - low) / span * 100).clip(lower=0, upper=100).fillna(50.0)

    tail5 = df_s.tail(5)
    idx5 = tail5.index
    a = bool(((vol_ratio.loc[idx5] < 0.8) & (close_pos.loc[idx5] > 60)).any())

    last_idx = df_s.index[-1]
    last_vr = _metric(vol_ratio.loc[last_idx])
    last_cp = _metric(close_pos.loc[last_idx])
    b = bool(last_vr is not None and last_cp is not None and last_vr >= 1.5 and last_cp > 70)

    # C 要测的是「低点反复受支撑考验」，所以基准必须是支撑位。
    # compute_support_level 对 sos 返回的是 21 日最高价（突破参考位／阻力位），
    # 拿最低价去比它在语义上不成立：实测 sos 的 C 命中率 45.8%、其余信号 96.9%，
    # 差异并非结构差别而是口径污染——那 8.8% 的样本命中与否纯属偶然。
    # 故 C 统一用「近 20 日最低价」这一真支撑口径，与 signal_type 无关。
    # 注意不改 compute_support_level 本身：它同时供 support_level 输出与盘中诊断使用，
    # 改动会波及止损参考位，需另行评估。
    support = compute_support_level(df, _SPRINGBOARD_SUPPORT_SIGNAL, window)
    tol = support * 0.05
    touches, evidence = _springboard_evidence(df_s, vol_ratio, close_pos, low, support, tol, window)
    c = touches >= 2

    return _springboard_result(a, b, c, support, touches, evidence)


def build_snap(
    signal_type: str,
    df: pd.DataFrame,
    score: float,
    cfg: Any = None,
) -> dict[str, Any]:
    """从 OHLCV DataFrame 最后一根 K 线构建价格快照。"""
    df_s = df.sort_values("date") if "date" in df.columns else df
    last = df_s.iloc[-1]
    ma20 = float(df_s["close"].rolling(20).mean().iloc[-1]) if len(df_s) >= 20 else float(last["close"])
    ma50 = float(df_s["close"].rolling(50).mean().iloc[-1]) if len(df_s) >= 50 else float(last["close"])

    snap = {
        "snap_open": float(last["open"]),
        "snap_high": float(last["high"]),
        "snap_low": float(last["low"]),
        "snap_close": float(last["close"]),
        "snap_volume": float(last["volume"]),
        "snap_ma20": ma20,
        "snap_ma50": ma50,
    }

    if signal_type in ("spring", "compression"):
        window = 60 if cfg is None else getattr(cfg, "spring_support_window", 60)
        zone = df_s.iloc[-(window + 2) : -2] if len(df_s) > window + 2 else df_s.iloc[:-2]
        snap["snap_support"] = float(zone["close"].min()) if len(zone) > 0 else float(last["low"])
    elif signal_type == "sos":
        snap["snap_support"] = float(df_s["high"].tail(21).iloc[:-1].max()) if len(df_s) >= 21 else float(last["high"])
    elif signal_type in {
        "lps",
        "trend_pullback",
        "trend_lane_pullback",
        "mainline",
        "main_force_entry",
    }:
        snap["snap_support"] = ma20
    elif signal_type in {"trend_breakout", "sector_strength", "wyckoff_structure"}:
        snap["snap_support"] = max(float(last["low"]), ma20 * 0.985)
    else:
        snap["snap_support"] = float(last["low"])

    return snap


def has_bar_on(df: pd.DataFrame | None, trade_date: str) -> bool:
    """df 最后一根 K 线是否正好是 trade_date。

    实盘不做 as-of 裁切,停牌股会留着停牌前那根旧 bar；拿它当"今天"去判确认,
    等于用过期价格决定信号生死。
    """
    if df is None or getattr(df, "empty", True) or "date" not in df.columns:
        return False
    df_s = df.sort_values("date")
    return str(df_s["date"].iloc[-1])[:10] == str(trade_date)[:10]


def build_today_ohlcv(df: pd.DataFrame) -> dict[str, float]:
    """从 DataFrame 最后一根 K 线构建 today_ohlcv dict。"""
    df_s = df.sort_values("date") if "date" in df.columns else df
    last = df_s.iloc[-1]
    ma20 = float(df_s["close"].rolling(20).mean().iloc[-1]) if len(df_s) >= 20 else float(last["close"])
    ma50 = float(df_s["close"].rolling(50).mean().iloc[-1]) if len(df_s) >= 50 else float(last["close"])
    return {
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "close": float(last["close"]),
        "volume": float(last["volume"]),
        "ma20": ma20,
        "ma50": ma50,
    }


def _confirmed_symbol_info(sig: dict, code_str: str, today: dict[str, float], trade_date: str, reason: str) -> dict:
    signal_type = sig["signal_type"]
    item = {
        "code": code_str,
        "name": sig.get("name", code_str),
        "tag": f"{signal_type.upper()}(跨日确认)",
        "track": "Accum" if signal_type in ("spring", "lps", "compression") else "Trend",
        "initial_price": today["close"],
        "score": sig.get("signal_score", 0),
        "signal_type": signal_type,
        "status": "confirmed",
        "signal_status": "confirmed",
        "selection_source": "signal_confirmed",
        "source_type": "signal_pending",
        "signal_date": str(sig["signal_date"]),
        "confirm_date": trade_date,
        "confirm_reason": reason,
        "support_level": sig.get("snap_support", today["close"]),
    }
    _copy_candidate_fields(item, sig)
    return item


def _copy_candidate_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in (
        "strategy_version",
        "candidate_lane",
        "entry_type",
        "signal_key",
        "candidate_status",
        "candidate_timing",
        "candidate_risk",
        "candidate_reasons",
        "candidate_metrics",
        "mainline_score",
        "theme_score",
        "stock_role_score",
        "quality_score",
        "timing_score",
    ):
        if source.get(key) not in (None, "", [], {}):
            target[key] = source[key]


def _pending_code_key(code: Any) -> str:
    """还原 PendingPool.write 存储的 df_map key：A 股数字代码补零，港股/美股代码原样保留。"""
    if isinstance(code, int):
        return f"{code:06d}"
    return str(code)


# 取全市场日历时每只票只看尾部这么多根：max(TTL) 之外再留几天缓冲，
# 避免为了几个日期去 union 五千多个 DataFrame 的全部行。
_CALENDAR_TAIL_BARS = 12


def market_trade_calendar(df_map: dict[str, pd.DataFrame], trade_date: str) -> list[str]:
    """从 df_map 里汇出 <= trade_date 的全市场交易日(升序)。

    TTL 以「交易日」计价，不能按函数调用次数计价：同一个 trade_date 被跑两轮
    (人工重跑、周日补跑) 时，逐次自增会凭空烧掉一天确认窗。这里用全市场日历
    重算 days_elapsed，让一天内跑几轮都得到同一个结果。
    """
    end = str(trade_date)[:10]
    days: set[str] = set()
    for frame in df_map.values():
        if frame is None or getattr(frame, "empty", True) or "date" not in frame.columns:
            continue
        col = frame["date"].astype(str).str.slice(0, 10)
        # 先按 trade_date 截断再取尾：反过来的话,调用方传进越过 trade_date 的
        # frame 会让日历被过滤空,静默退回自然日,周一就会把 1 个交易日算成 3 天。
        days.update(col[col <= end].tail(_CALENDAR_TAIL_BARS))
    days.discard("")
    return sorted(days)


def elapsed_trade_days(calendar: list[str], signal_date: str, trade_date: str) -> int:
    """信号日之后、trade_date 之前(含)经过了几个交易日。

    停牌不影响计数：日历取自全市场,个股缺 bar 只该让它不可判定,不该让它不老化。
    """
    start, end = str(signal_date)[:10], str(trade_date)[:10]
    if not start or not end or end <= start:
        return 0
    if calendar:
        # 日历非空但窗口内没有交易日 → trade_date 不是交易日,不该老化。
        return sum(1 for day in calendar if start < day <= end)
    # 日历为空(df_map 为空)时退回自然日,至少保证 TTL 能到期而不是挂死。
    try:
        delta = (datetime.fromisoformat(end).date() - datetime.fromisoformat(start).date()).days
    except ValueError:
        return 0
    return max(delta, 0)


def run_confirmation_cycle(
    pending_signals: list[dict],
    df_map: dict[str, pd.DataFrame],
    trade_date: str,
) -> tuple[list[dict], list[dict]]:
    """对一批 pending 信号执行确认/过期判定，返回 (updates, confirmed_symbols)。"""
    updates: list[dict] = []
    confirmed_symbols: list[dict] = []
    calendar = market_trade_calendar(df_map, trade_date)

    for sig in pending_signals:
        # days_elapsed 按全市场交易日重算,不按调用次数自增：同一个 trade_date
        # 跑第二轮时结果不变,信号日当天算出 0 天,天然跳过(当天 K 线 == 快照)。
        days = elapsed_trade_days(calendar, sig.get("signal_date", ""), trade_date)
        if days <= 0:
            continue

        code_str = _pending_code_key(sig["code"])
        df = df_map.get(code_str)
        if not has_bar_on(df, trade_date):
            # 停牌/缺数据：今天判不了确认,但 TTL 照走,不能挂在池子里永不终结。
            ttl = SIGNAL_TTL_DAYS.get(sig["signal_type"], 3)
            if days < ttl:
                continue
            updates.append(
                {
                    "id": sig["id"],
                    "status": "expired",
                    "days_elapsed": days,
                    "confirm_reason": f"TTL {ttl}天已到，期间无可用K线（停牌或缺数据）",
                    "expire_date": trade_date,
                }
            )
            continue

        today = build_today_ohlcv(df)
        snap = {k: sig[k] for k in sig if k.startswith("snap_")}
        new_status, reason = check_confirmation(sig["signal_type"], snap, today, days)

        update: dict[str, Any] = {
            "id": sig["id"],
            "status": new_status,
            "days_elapsed": days,
            "confirm_reason": reason,
        }
        if new_status == "confirmed":
            update["confirm_date"] = trade_date
            confirmed_symbols.append(_confirmed_symbol_info(sig, code_str, today, trade_date, reason))
        elif new_status == "expired":
            update["expire_date"] = trade_date
        updates.append(update)

    return updates, confirmed_symbols


class PendingPool:
    """signal_pending 的内存模拟，用于回测。"""

    def __init__(self) -> None:
        self._pool: dict[tuple[str, str], dict] = {}
        self._next_id: int = 1

    def write(
        self,
        signal_date: str,
        triggers: dict[str, list[tuple[str, float]]],
        df_map: dict[str, pd.DataFrame],
        regime: str = "NEUTRAL",
        name_map: dict[str, str] | None = None,
        sector_map: dict[str, str] | None = None,
        cfg: Any = None,
    ) -> int:
        name_map, sector_map = name_map or {}, sector_map or {}
        added = 0
        for signal_type, hits in triggers.items():
            ttl = SIGNAL_TTL_DAYS.get(signal_type, 3)
            for code, score in hits:
                key = (code, signal_type)
                if key in self._pool:
                    continue
                df = df_map.get(code)
                if df is None or df.empty:
                    continue
                snap = build_snap(signal_type, df, score, cfg)
                self._pool[key] = {
                    "id": self._next_id,
                    "code": int(code) if code.isdigit() else code,
                    "signal_type": signal_type,
                    "signal_date": signal_date,
                    "signal_score": score,
                    "status": "pending",
                    "ttl_days": ttl,
                    "days_elapsed": 0,
                    "regime": regime,
                    "name": name_map.get(code, code),
                    "industry": sector_map.get(code, ""),
                    **snap,
                }
                self._next_id += 1
                added += 1
        return added

    def tick(self, df_map: dict[str, pd.DataFrame], trade_date: str) -> list[dict]:
        """推进一天，返回确认通过的 symbol_info 列表。"""
        if not self._pool:
            return []
        updates, confirmed = run_confirmation_cycle(list(self._pool.values()), df_map, trade_date)
        for upd in updates:
            if upd["status"] in ("confirmed", "expired"):
                for key, sig in list(self._pool.items()):
                    if sig["id"] == upd["id"]:
                        del self._pool[key]
                        break
            else:
                for sig in self._pool.values():
                    if sig["id"] == upd["id"]:
                        sig["status"] = upd["status"]
                        sig["days_elapsed"] = upd["days_elapsed"]
                        sig["confirm_reason"] = upd.get("confirm_reason", "")
                        break
        return confirmed
