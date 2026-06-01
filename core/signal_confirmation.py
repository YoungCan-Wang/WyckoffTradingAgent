"""信号确认逻辑：pending → confirmed / expired。纯业务，不依赖 DB。"""

from __future__ import annotations

from typing import Any

import pandas as pd

SIGNAL_TTL_DAYS: dict[str, int] = {
    "sos": 2,
    "spring": 3,
    "lps": 3,
    "evr": 2,
    "compression": 3,
}


def check_confirmation(
    signal_type: str,
    snap: dict[str, Any],
    today_ohlcv: dict[str, float],
    days_elapsed: int,
) -> tuple[str, str]:
    """返回 (new_status, reason)，status ∈ {'pending', 'confirmed', 'expired'}。"""
    ttl = SIGNAL_TTL_DAYS.get(signal_type, 3)
    if days_elapsed >= ttl:
        return "expired", f"TTL {ttl}天已到，未满足确认条件"
    fn = _CONFIRM_DISPATCH.get(signal_type)
    if fn is None:
        return "expired", f"未知信号类型: {signal_type}"
    return fn(snap, today_ohlcv, days_elapsed)


def _confirm_sos(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    snap_low, snap_close, snap_vol = snap.get("snap_low", 0), snap.get("snap_close", 0), snap.get("snap_volume", 0)
    if today["low"] < snap_low:
        return "expired", f"跌破信号日低点 {snap_low:.2f}"
    if snap_vol > 0 and today["volume"] > snap_vol * 0.8 and today["close"] < snap_close * 0.97:
        return "expired", "放量回落，非缩量确认"
    if (
        snap_vol > 0
        and today["volume"] < snap_vol * 0.8
        and today["low"] >= snap_low
        and today["close"] >= snap_close * 0.97
    ):
        return "confirmed", f"缩量回踩确认，收盘 {today['close']:.2f} 守住 {snap_low:.2f}"
    return "pending", "等待缩量确认"


def _confirm_spring(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    support, snap_ma20 = snap.get("snap_support", 0), snap.get("snap_ma20", 0)
    if today["low"] < support * 0.98:
        return "expired", f"跌破支撑 {support:.2f}"
    if today["close"] > support and today["close"] >= snap_ma20 * 0.97:
        return "confirmed", f"守住支撑 {support:.2f}，收盘接近 MA20"
    return "pending", "等待收回 MA20"


def _confirm_lps(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    snap_ma20, snap_vol = snap.get("snap_ma20", 0), snap.get("snap_volume", 0)
    if today["low"] < snap_ma20 * 0.98:
        return "expired", f"跌破 MA20 {snap_ma20:.2f}"
    if snap_vol > 0 and today["volume"] > snap_vol * 1.5:
        return "expired", "异常放量，LPS 逻辑失效"
    if today["close"] >= snap_ma20 and (snap_vol <= 0 or today["volume"] <= snap_vol * 1.2):
        return "confirmed", f"站稳 MA20 {snap_ma20:.2f}，缩量确认"
    return "pending", "等待站稳 MA20"


def _confirm_evr(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    event_low, snap_close = snap.get("snap_support", 0), snap.get("snap_close", 0)
    if today["close"] < event_low:
        return "expired", f"跌破事件日低点 {event_low:.2f}"
    if today["close"] >= event_low and today["close"] >= snap_close * 0.98:
        return "confirmed", f"守住 {event_low:.2f}，收盘 {today['close']:.2f}"
    return "pending", "等待企稳确认"


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


_CONFIRM_DISPATCH = {
    "sos": _confirm_sos,
    "spring": _confirm_spring,
    "lps": _confirm_lps,
    "evr": _confirm_evr,
    "compression": _confirm_compression,
}


def _compute_support_level(
    df: pd.DataFrame,
    signal_type: str,
    window: int = 60,
) -> float:
    """根据信号类型计算支撑位。"""
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
    vol_ratio = volume / vol_ma20.replace(0, pd.NA)
    span = (high - low).replace(0, pd.NA)
    close_pos = ((close - low) / span * 100).clip(lower=0, upper=100).fillna(50.0)

    tail5 = df_s.tail(5)
    idx5 = tail5.index
    a = bool(((vol_ratio.loc[idx5] < 0.8) & (close_pos.loc[idx5] > 60)).any())

    last_idx = df_s.index[-1]
    last_vr = _metric(vol_ratio.loc[last_idx])
    last_cp = _metric(close_pos.loc[last_idx])
    b = bool(last_vr is not None and last_cp is not None and last_vr >= 1.5 and last_cp > 70)

    support = _compute_support_level(df, signal_type, window)
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
    elif signal_type == "lps":
        snap["snap_support"] = ma20
    else:
        snap["snap_support"] = float(last["low"])

    return snap


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


def _missing_ohlcv_update(sig: dict, trade_date: str) -> dict[str, Any]:
    days = int(sig.get("days_elapsed") or 0) + 1
    ttl = int(sig.get("ttl_days") or SIGNAL_TTL_DAYS.get(str(sig.get("signal_type") or ""), 3))
    expired = days >= ttl
    update: dict[str, Any] = {
        "id": sig["id"],
        "status": "expired" if expired else "pending",
        "days_elapsed": days,
        "confirm_reason": f"OHLCV 缺失，TTL {ttl}天已到" if expired else "OHLCV 缺失，等待下次确认",
    }
    if expired:
        update["expire_date"] = trade_date
    return update


def run_confirmation_cycle(
    pending_signals: list[dict],
    df_map: dict[str, pd.DataFrame],
    trade_date: str,
) -> tuple[list[dict], list[dict]]:
    """对一批 pending 信号执行确认/过期判定，返回 (updates, confirmed_symbols)。"""
    updates: list[dict] = []
    confirmed_symbols: list[dict] = []

    for sig in pending_signals:
        # 信号日当天不做确认检查：当天 K 线 == 信号快照，无法验证"次日回踩"
        if str(sig.get("signal_date", ""))[:10] == str(trade_date)[:10]:
            continue

        code_str = f"{int(sig['code']):06d}"
        df = df_map.get(code_str)
        if df is None or df.empty:
            updates.append(_missing_ohlcv_update(sig, trade_date))
            continue

        days = sig.get("days_elapsed", 0) + 1
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
            confirmed_symbols.append(
                {
                    "code": code_str,
                    "name": sig.get("name", code_str),
                    "tag": f"{sig['signal_type'].upper()}(确认)",
                    "track": "Accum" if sig["signal_type"] in ("spring", "lps") else "Trend",
                    "initial_price": today["close"],
                    "score": sig.get("signal_score", 0),
                    "signal_type": sig["signal_type"],
                    "signal_date": str(sig["signal_date"]),
                }
            )
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
                        sig["days_elapsed"] = upd["days_elapsed"]
                        break
        return confirmed
