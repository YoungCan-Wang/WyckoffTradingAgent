"""Single-symbol daily funnel replay diagnosis workflow."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from core.candidate_lanes import build_l1_candidate_lane_entries
from core.candidate_policy import candidate_score_value, loss_guard_reason
from core.candidate_ranker import TRIGGER_LABELS, TRIGGER_SHORT_LABELS
from core.cn_boards import is_supported_cn_board
from core.signal_confirmation import score_springboard_abc
from core.wyckoff_engine import (
    FunnelConfig,
    layer1_filter,
    layer2_strength_detailed,
    layer3_sector_resonance,
    layer4_triggers,
)
from workflows.market_funnel_config import funnel_config_for_market
from workflows.single_symbol_diagnosis_data import (
    fetch_symbol_history,
    load_rps_universe_histories,
    load_symbol_context,
)
from workflows.single_symbol_diagnosis_outputs import notify_single_symbol_feishu, write_single_symbol_outputs

MIN_RPS_UNIVERSE_HISTORIES = 500


@dataclass(frozen=True)
class SymbolSpec:
    market: str
    symbol: str
    label: str


@dataclass(frozen=True)
class ReplayContext:
    name_map: dict[str, str]
    market_cap_map: dict[str, float]
    sector_map: dict[str, str]
    bench_df: pd.DataFrame | None


@dataclass
class DayDiagnostic:
    date: str
    status: str
    failed_layer: str
    reason: str
    triggers: str
    trigger_scores: str
    abc_grade: str
    abc_count: int
    channel: str
    close: float | None
    pct_chg: float | None
    vol_ratio: float | None
    amount_avg_wan: float | None
    ma50: float | None
    ma200: float | None


@dataclass(frozen=True)
class SingleSymbolDiagnosisRequest:
    symbol: str
    start_date: str
    end_date: str
    trading_days: int = 320
    output_dir: Path = Path("logs/single_symbol_funnel")
    skip_rps_universe: bool = False


def run_single_symbol_diagnosis(request: SingleSymbolDiagnosisRequest) -> int:
    start, end = parse_date_range(request.start_date, request.end_date)
    specs = parse_symbols(request.symbol)
    cfg = config_for_symbol(specs[0], int(request.trading_days))
    rps_histories = load_required_rps_histories(specs[0], start, end, cfg, request.skip_rps_universe)
    failed = 0
    for spec in specs:
        output_dir = request.output_dir / spec.symbol if len(specs) > 1 else request.output_dir
        failed += run_single_symbol(spec, start, end, int(request.trading_days), output_dir, rps_histories)
    return min(failed, 1)


def detect_symbol_market(raw_symbol: str) -> SymbolSpec:
    raw = str(raw_symbol or "").strip().upper()
    if not raw:
        raise ValueError("symbol 不能为空")
    if re.fullmatch(r"\d{6}(\.(SH|SZ|BJ))?", raw):
        return SymbolSpec("cn", raw.split(".", 1)[0], "A股")
    if raw.endswith(".HK") or re.fullmatch(r"\d{1,5}", raw):
        return SymbolSpec("hk", f"{raw.replace('.HK', '').zfill(5)}.HK", "港股")
    if raw.endswith(".US"):
        return SymbolSpec("us", raw, "美股")
    if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", raw):
        return SymbolSpec("us", f"{raw}.US", "美股")
    raise ValueError(f"无法识别股票市场: {raw_symbol}")


def parse_ymd(value: str) -> date:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期格式必须是 YYYY-MM-DD: {value}") from exc


def parse_date_range(start_date: str, end_date: str) -> tuple[date, date]:
    start, end = parse_ymd(start_date), parse_ymd(end_date)
    if end < start:
        raise ValueError("end-date 不能早于 start-date")
    return start, end


def parse_symbols(raw: str) -> list[SymbolSpec]:
    specs = [detect_symbol_market(s) for s in str(raw).replace(";", ",").split(",") if s.strip()]
    if not specs:
        raise ValueError("symbol 不能为空")
    markets = {s.market for s in specs}
    if len(markets) > 1:
        raise ValueError(f"批量模式要求所有股票属于同一市场，当前包含: {', '.join(sorted(markets))}")
    return specs


def build_context(spec: SymbolSpec, hist: pd.DataFrame, start: date, end: date) -> ReplayContext:
    data = load_symbol_context(spec, hist, start, end, log=lambda msg: print(msg, flush=True))
    return ReplayContext(data.name_map, data.market_cap_map, data.sector_map, data.bench_df)


def config_for_symbol(spec: SymbolSpec, trading_days: int) -> FunnelConfig:
    if spec.market == "cn":
        return FunnelConfig(trading_days=trading_days)
    return funnel_config_for_market(spec.market, trading_days=trading_days)


def replay_symbol(
    spec: SymbolSpec,
    hist: pd.DataFrame,
    ctx: ReplayContext,
    cfg: FunnelConfig,
    start: date,
    end: date,
    rps_histories: dict[str, pd.DataFrame] | None = None,
) -> list[DayDiagnostic]:
    days = [x for x in hist["date_obj"].tolist() if start <= x <= end]
    return [evaluate_day(spec, hist[hist["date_obj"] <= day].copy(), ctx, cfg, day, rps_histories) for day in days]


def evaluate_day(
    spec: SymbolSpec,
    day_df: pd.DataFrame,
    ctx: ReplayContext,
    cfg: FunnelConfig,
    day: date,
    rps_histories: dict[str, pd.DataFrame] | None = None,
) -> DayDiagnostic:
    if len(day_df) < max(60, min(cfg.trading_days, 220)):
        return diagnostic(spec, day_df, day, "MISS", "DATA", "历史数据不足，无法完成漏斗回放", {}, "")
    df_map = {spec.symbol: day_df}
    layer1 = layer1_filter([spec.symbol], ctx.name_map, ctx.market_cap_map, df_map, cfg)
    if spec.symbol not in layer1:
        return diagnostic(spec, day_df, day, "MISS", "L1", l1_reason(spec, day_df, ctx, cfg), {}, "")
    rps_df_map, rps_universe = build_rps_context(df_map, rps_histories, day)
    layer2, channel_map, _ = layer2_strength_detailed(
        layer1, rps_df_map, benchmark_for_day(ctx.bench_df, day), cfg, rps_universe=rps_universe
    )
    if spec.symbol not in layer2:
        lane_scores = candidate_lane_scores(spec, day_df, ctx, layer1, [], {})
        if lane_scores:
            return selected_or_guarded_diagnostic(spec, day_df, day, lane_scores, "")
        return diagnostic(spec, day_df, day, "MISS", "L2", l2_reason(spec, day_df, cfg), {}, "")
    return evaluate_l3_l4(spec, day_df, ctx, cfg, day, layer1, layer2, channel_map)


def evaluate_l3_l4(
    spec: SymbolSpec,
    day_df: pd.DataFrame,
    ctx: ReplayContext,
    cfg: FunnelConfig,
    day: date,
    layer1: list[str],
    layer2: list[str],
    channel_map: dict[str, str],
) -> DayDiagnostic:
    df_map = {spec.symbol: day_df}
    channel = channel_map.get(spec.symbol, "")
    layer3, _ = layer3_sector_resonance(layer2, ctx.sector_map, cfg, base_symbols=layer1, df_map=df_map)
    if spec.symbol not in layer3:
        return diagnostic(spec, day_df, day, "MISS", "L3", l3_reason(spec, ctx), {}, channel)
    scores = trigger_scores(layer4_triggers(layer3, df_map, cfg, channel_map), spec.symbol)
    scores.update(candidate_lane_scores(spec, day_df, ctx, layer1, layer2, channel_map))
    if not scores:
        return diagnostic(spec, day_df, day, "MISS", "L4", l4_reason(day_df), scores, channel)
    return selected_or_guarded_diagnostic(spec, day_df, day, scores, channel)


def candidate_lane_scores(
    spec: SymbolSpec,
    day_df: pd.DataFrame,
    ctx: ReplayContext,
    layer1: list[str],
    layer2: list[str],
    channel_map: dict[str, str],
) -> dict[str, float]:
    entries = build_l1_candidate_lane_entries(
        l1_symbols=layer1,
        df_map={spec.symbol: day_df},
        sector_map=ctx.sector_map,
        top_sectors=[],
        l2_symbols=layer2,
        channel_map=channel_map,
        limit=10,
    )
    return {
        str(item.get("signal_key") or item.get("entry_type") or "candidate_lane"): candidate_score_value(
            item.get("score")
        )
        for item in entries
        if str(item.get("code", "")).strip() == spec.symbol
    }


def build_rps_context(
    df_map: dict[str, pd.DataFrame],
    rps_histories: dict[str, pd.DataFrame] | None,
    day: date,
) -> tuple[dict[str, pd.DataFrame], list[str] | None]:
    if not rps_histories:
        return df_map, None
    merged: dict[str, pd.DataFrame] = dict(df_map)
    day_str = day.isoformat()
    for sym, full_df in rps_histories.items():
        if sym not in merged:
            sliced = full_df[full_df["date"] <= day_str]
            if len(sliced) >= 50:
                merged[sym] = sliced
    return merged, list(merged.keys())


def benchmark_for_day(bench_df: pd.DataFrame | None, day: date) -> pd.DataFrame | None:
    if bench_df is None or bench_df.empty:
        return bench_df
    return bench_df[bench_df["date"] <= day.isoformat()].copy()


def trigger_scores(triggers: dict[str, list[tuple[str, float]]], symbol: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for trigger, rows in triggers.items():
        for code, score in rows:
            if code == symbol:
                out[trigger] = max(out.get(trigger, 0.0), candidate_score_value(score))
    return out


def selected_or_guarded_diagnostic(
    spec: SymbolSpec,
    day_df: pd.DataFrame,
    day: date,
    scores: dict[str, float],
    channel: str,
) -> DayDiagnostic:
    reason = diagnostic_loss_guard_reason(spec, day_df, scores, channel)
    if reason:
        return diagnostic(spec, day_df, day, "MISS", "LOSS_GUARD", reason, scores, channel)
    return diagnostic(spec, day_df, day, "SELECTED", "-", selected_reason(scores, day_df), scores, channel)


def diagnostic_loss_guard_reason(
    spec: SymbolSpec,
    day_df: pd.DataFrame,
    scores: dict[str, float],
    channel: str,
) -> str:
    if spec.market != "cn" or not scores:
        return ""
    reason = loss_guard_reason(
        spec.symbol,
        "NEUTRAL",
        scores.keys(),
        sum(float(v or 0.0) for v in scores.values()),
        channel,
        {spec.symbol: day_df},
    )
    return f"正式候选风控拦截: {reason}" if reason else ""


def diagnostic(
    spec: SymbolSpec,
    day_df: pd.DataFrame,
    day: date,
    status: str,
    failed_layer: str,
    reason: str,
    scores: dict[str, float],
    channel: str,
) -> DayDiagnostic:
    metrics = day_metrics(day_df)
    abc = best_abc(day_df, list(scores))
    return DayDiagnostic(
        date=day.isoformat(),
        status=status,
        failed_layer=failed_layer,
        reason=reason,
        triggers="+".join(TRIGGER_SHORT_LABELS.get(key, key) for key in scores) if scores else "-",
        trigger_scores=", ".join(f"{key}:{value:.2f}" for key, value in scores.items()) if scores else "-",
        abc_grade=str(abc.get("grade", "none")),
        abc_count=int(abc.get("met_count", 0)),
        channel=channel or "-",
        close=metrics["close"],
        pct_chg=metrics["pct_chg"],
        vol_ratio=metrics["vol_ratio"],
        amount_avg_wan=metrics["amount_avg_wan"],
        ma50=metrics["ma50"],
        ma200=metrics["ma200"],
    )


def best_abc(day_df: pd.DataFrame, trigger_keys: list[str]) -> dict[str, Any]:
    scored = [score_springboard_abc(day_df, key) for key in (trigger_keys or list(TRIGGER_LABELS))]
    return max(scored, key=lambda item: (int(item.get("met_count", 0)), str(item.get("grade", ""))))


def day_metrics(df: pd.DataFrame) -> dict[str, float | None]:
    close = pd.to_numeric(df["close"], errors="coerce")
    volume = numeric_series(df, "volume")
    amount = numeric_series(df, "amount")
    last = df.iloc[-1] if not df.empty else {}
    vol_ma20 = volume.rolling(20).mean().iloc[-1] if len(volume) >= 20 else None
    return {
        "close": float_or_none(last.get("close")),
        "pct_chg": float_or_none(last.get("pct_chg")),
        "vol_ratio": safe_ratio(float_or_none(last.get("volume")), float_or_none(vol_ma20)),
        "amount_avg_wan": float_or_none(amount.tail(20).mean() / 10000) if not amount.empty else None,
        "ma50": float_or_none(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None,
        "ma200": float_or_none(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None,
    }


def numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(df[column], errors="coerce")


def float_or_none(value: Any) -> float | None:
    try:
        if pd.notna(value):
            return float(value)
    except (TypeError, ValueError):
        return None
    return None


def safe_ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def l1_reason(spec: SymbolSpec, df: pd.DataFrame, ctx: ReplayContext, cfg: FunnelConfig) -> str:
    name = ctx.name_map.get(spec.symbol, "")
    if (
        cfg.require_cn_main_or_chinext
        and spec.market == "cn"
        and not is_supported_cn_board(
            spec.symbol,
            include_bse=cfg.include_bse_board,
        )
    ):
        return "非A股目标板块标的，不在 A 股漏斗股票池"
    if "ST" in name.upper():
        return "名称包含 ST，被 Layer1 硬过滤"
    avg_amount = day_metrics(df)["amount_avg_wan"]
    cap = ctx.market_cap_map.get(spec.symbol)
    if cap is not None and cap < cfg.min_market_cap_yi:
        return f"总市值 {cap:.1f}亿低于阈值 {cfg.min_market_cap_yi:.1f}亿"
    if avg_amount is not None and avg_amount < cfg.min_avg_amount_wan:
        return f"20日均成交额 {avg_amount:.0f}万低于阈值 {cfg.min_avg_amount_wan:.0f}万"
    return "未通过 Layer1 硬过滤，通常是标的范围/成交额/市值条件不满足"


def l2_reason(spec: SymbolSpec, df: pd.DataFrame, cfg: FunnelConfig) -> str:
    metrics = day_metrics(df)
    if metrics["ma200"] is None:
        return "历史不足 200 日，主升/吸筹/RPS 等 Layer2 指标不完整"
    if metrics["close"] and metrics["ma50"] and metrics["close"] < metrics["ma50"]:
        return f"收盘 {metrics['close']:.2f} 低于 MA50 {metrics['ma50']:.2f}，强弱通道不足"
    if spec.market == "cn" and cfg.enable_rps_filter:
        return "未通过 Layer2 八通道，常见原因是 RPS/相对强弱或吸筹结构不足"
    return "未通过 Layer2 八通道（主升/潜伏/吸筹/地量/护盘/趋势延续/加速突破/点火）"


def l3_reason(spec: SymbolSpec, ctx: ReplayContext) -> str:
    sector = ctx.sector_map.get(spec.symbol, "")
    suffix = f"，当前行业/概念={sector}" if sector else ""
    return f"Layer3 板块/概念共振不足{suffix}"


def l4_reason(df: pd.DataFrame) -> str:
    abc = best_abc(df, [])
    return f"通过前置层，但未触发正式 Spring/SOS/LPS/EVR/Compression；ABC={abc['grade']}({abc['met_count']}/3)"


def selected_reason(scores: dict[str, float], df: pd.DataFrame) -> str:
    abc = best_abc(df, list(scores))
    labels = [f"{TRIGGER_LABELS.get(key, key)} {value:.2f}" for key, value in scores.items()]
    return f"触发 {' + '.join(labels)}；ABC={abc['grade']}({abc['met_count']}/3)"


def summarize_diagnostics(rows: list[DayDiagnostic]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        key = row.status if row.status == "SELECTED" else row.failed_layer
        counts[key] = counts.get(key, 0) + 1
    selected = [row for row in rows if row.status == "SELECTED"]
    return {
        "total_days": len(rows),
        "selected_days": len(selected),
        "counts": counts,
        "first_selected": selected[0].date if selected else None,
        "last_selected": selected[-1].date if selected else None,
    }


def run_single_symbol(
    spec: SymbolSpec,
    start: date,
    end: date,
    trading_days: int,
    output_dir: Path,
    rps_histories: dict[str, pd.DataFrame] | None,
) -> int:
    print(f"[diagnosis] symbol={spec.symbol} market={spec.market} range={start}~{end}", flush=True)
    hist = fetch_symbol_history(spec, start, end, trading_days)
    if hist.empty:
        print(f"[diagnosis] {spec.symbol} 没有获取到可用历史数据，跳过", flush=True)
        return 1
    ctx = build_context(spec, hist, start, end)
    cfg = config_for_symbol(spec, trading_days)
    rows = replay_symbol(spec, hist, ctx, cfg, start, end, rps_histories)
    summary = summarize_diagnostics(rows)
    paths = write_single_symbol_outputs(output_dir, spec, rows, summary)
    notify_single_symbol_feishu(spec, summary, paths["md"])
    print(f"[diagnosis] done {spec.symbol} selected={summary['selected_days']} report={paths['md']}", flush=True)
    return 0


def load_required_rps_histories(
    spec: SymbolSpec, start: date, end: date, cfg: FunnelConfig, skip_rps: bool
) -> dict[str, pd.DataFrame] | None:
    if skip_rps or spec.market != "cn" or not cfg.enable_rps_filter:
        return None
    histories = load_rps_universe_histories(
        spec, start, end, cfg.rps_window_slow + 30, log=lambda msg: print(msg, flush=True)
    )
    if len(histories) < MIN_RPS_UNIVERSE_HISTORIES:
        raise RuntimeError(
            f"RPS 全市场历史不足（{len(histories)}/{MIN_RPS_UNIVERSE_HISTORIES}），"
            "无法生成可信截面排名；如需快速近似请显式使用 --skip-rps-universe"
        )
    return histories
