"""
Wyckoff Funnel 定时任务：5 层漏斗筛选 → 多渠道推送

Layer 1: 剥离垃圾（ST/北交所/科创板/市值/成交额）
Layer 2: 七通道甄选（主升/潜伏/吸筹/地量/暗中护盘/趋势延续/点火破局）
Layer 2.5: Markup 加速检测
Layer 3: 板块共振（行业 Top-N）
Layer 4: 威科夫狙击（Spring / SOS / LPS / Effort vs Result）
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.dynamic_policy import (
    build_signal_weight_map,
    dynamic_policy_mode,
    filter_triggers_by_registry,
    resolve_dynamic_candidate_policy,
)
from core.sector_rotation import analyze_sector_rotation
from core.theme_radar import build_theme_radar_snapshot, summarize_theme_radar
from core.wyckoff_engine import (
    FunnelConfig,
    FunnelResult,
    allocate_ai_candidates,
    detect_accum_stage,
    detect_markup_stage,
    layer1_filter,
    layer2_strength_detailed,
    layer3_sector_resonance,
    layer4_triggers,
    layer5_exit_signals,
    resolve_ai_candidate_policy,
)
from integrations.data_source import (
    _CONCEPT_HEAT_HISTORY,
    detect_theme_lines,
    fetch_concept_heat,
    fetch_concept_map,
    fetch_index_hist,
    fetch_market_cap_map,
    fetch_sector_map,
    update_concept_heat_history,
)
from integrations.fetch_a_share_csv import (
    _resolve_trading_window,
)
from integrations.supabase_signal_feedback import (
    load_signal_health_snapshot,
    load_signal_registry,
    upsert_policy_shadow_run,
)
from integrations.tickflow_notice import TICKFLOW_UPGRADE_URL

# ── tools/ 层导入 ──
from tools.candidate_ranker import (
    TRIGGER_GROUP_ORDER,
    TRIGGER_GROUP_TITLES,
    TRIGGER_LABELS,
    TRIGGER_SHORT_LABELS,
    calc_close_return_pct,
)
from tools.candidate_ranker import (
    rank_l3_candidates as _rank_l3_candidates,
)
from utils.feishu import send_feishu_notification
from utils.trading_clock import CN_TZ, resolve_end_calendar_day

logger = logging.getLogger(__name__)

TRADING_DAYS = int(os.getenv("FUNNEL_TRADING_DAYS", "320"))
MAX_RETRIES = int(os.getenv("FUNNEL_FETCH_RETRIES", "2"))
RETRY_BASE_DELAY = float(os.getenv("FUNNEL_RETRY_BASE_DELAY", "1.0"))
SOCKET_TIMEOUT = int(os.getenv("FUNNEL_SOCKET_TIMEOUT", "20"))
FETCH_TIMEOUT = int(os.getenv("FUNNEL_FETCH_TIMEOUT", "45"))
BATCH_TIMEOUT = int(os.getenv("FUNNEL_BATCH_TIMEOUT", "420"))
BATCH_SIZE = int(os.getenv("FUNNEL_BATCH_SIZE", "200"))
BATCH_SLEEP = float(os.getenv("FUNNEL_BATCH_SLEEP", "0.55"))
MAX_WORKERS = int(os.getenv("FUNNEL_MAX_WORKERS", "8"))
EXECUTOR_MODE = os.getenv("FUNNEL_EXECUTOR_MODE", "process").strip().lower()
if EXECUTOR_MODE not in {"thread", "process"}:
    EXECUTOR_MODE = "process"
ENFORCE_TARGET_TRADE_DATE = False
BREADTH_MA_WINDOW = int(os.getenv("FUNNEL_BREADTH_MA_WINDOW", "20"))
BREADTH_RISK_OFF_THRESHOLD = float(os.getenv("FUNNEL_BREADTH_RISK_OFF_PCT", "20.0"))
BREADTH_RISK_ON_THRESHOLD = float(os.getenv("FUNNEL_BREADTH_RISK_ON_PCT", "60.0"))
BREADTH_RISK_ON_MIN_DELTA = float(os.getenv("FUNNEL_BREADTH_RISK_ON_DELTA", "0.0"))
BREADTH_CLIFF_DROP_PCT = float(os.getenv("FUNNEL_BREADTH_CLIFF_DROP_PCT", "-10.0"))
SMALLCAP_BENCH_CODE = os.getenv("FUNNEL_SMALLCAP_BENCH_CODE", "399006").strip() or "399006"
CRASH_MAIN_DAY_DROP_PCT = float(os.getenv("FUNNEL_CRASH_MAIN_DAY_DROP_PCT", "-1.3"))
CRASH_SMALL_DAY_DROP_PCT = float(os.getenv("FUNNEL_CRASH_SMALL_DAY_DROP_PCT", "-2.5"))
CRASH_BREADTH_RATIO_PCT = float(os.getenv("FUNNEL_CRASH_BREADTH_RATIO_PCT", "15.0"))
CRASH_BREADTH_DELTA_PCT = float(os.getenv("FUNNEL_CRASH_BREADTH_DELTA_PCT", "-20.0"))
PANIC_REPAIR_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_PANIC_REPAIR_MIN_AVG_AMOUNT_WAN", "7000.0"))
RISK_OFF_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_RISK_OFF_MIN_AVG_AMOUNT_WAN", "8000.0"))
RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN", "10000.0"))
CRASH_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN", "12000.0"))
PANIC_REPAIR_ENABLE = os.getenv("FUNNEL_PANIC_REPAIR_ENABLE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PANIC_REPAIR_MAIN_REBOUND_PCT = float(os.getenv("FUNNEL_PANIC_REPAIR_MAIN_REBOUND_PCT", "0.8"))
PANIC_REPAIR_SMALL_REBOUND_PCT = float(os.getenv("FUNNEL_PANIC_REPAIR_SMALL_REBOUND_PCT", "1.5"))
FUNNEL_EXPORT_FULL_FETCH = os.getenv("FUNNEL_EXPORT_FULL_FETCH", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FUNNEL_EXPORT_DIR = os.getenv("FUNNEL_EXPORT_DIR", "data/funnel_snapshots").strip() or "data/funnel_snapshots"
FUNNEL_AI_SELECTION_MODE = os.getenv("FUNNEL_AI_SELECTION_MODE", "legacy_full_hits").strip().lower()
FUNNEL_CARD_STYLE = os.getenv("FUNNEL_CARD_STYLE", "legacy_compact").strip().lower()
FUNNEL_EVR_POLICY = os.getenv("FUNNEL_EVR_POLICY", "all_regimes").strip().lower()
try:
    FUNNEL_BYPASS_DISPLAY_LIMIT = max(int(float(os.getenv("FUNNEL_BYPASS_DISPLAY_LIMIT", "20"))), 0)
except Exception:
    logger.debug("FUNNEL_BYPASS_DISPLAY_LIMIT parse failed, using default", exc_info=True)
    FUNNEL_BYPASS_DISPLAY_LIMIT = 20
FUNNEL_L2_BYPASS_AI_ENABLED = os.getenv("FUNNEL_L2_BYPASS_AI_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
try:
    FUNNEL_L2_BYPASS_AI_CAP = max(int(float(os.getenv("FUNNEL_L2_BYPASS_AI_CAP", "30"))), 0)
except Exception:
    logger.debug("FUNNEL_L2_BYPASS_AI_CAP parse failed, using default", exc_info=True)
    FUNNEL_L2_BYPASS_AI_CAP = 30
try:
    FUNNEL_ETF_DISPLAY_LIMIT = max(int(float(os.getenv("FUNNEL_ETF_DISPLAY_LIMIT", "0"))), 0)
except Exception:
    logger.debug("FUNNEL_ETF_DISPLAY_LIMIT parse failed, using default", exc_info=True)
    FUNNEL_ETF_DISPLAY_LIMIT = 0
FUNNEL_THEME_RADAR_ENABLED = os.getenv("FUNNEL_THEME_RADAR_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FUNNEL_THEME_RADAR_LINK_ENABLED = os.getenv("FUNNEL_THEME_RADAR_LINK_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
try:
    FUNNEL_THEME_RADAR_PROMOTE_CAP = max(int(float(os.getenv("FUNNEL_THEME_RADAR_PROMOTE_CAP", "6"))), 0)
except Exception:
    logger.debug("FUNNEL_THEME_RADAR_PROMOTE_CAP parse failed, using default", exc_info=True)
    FUNNEL_THEME_RADAR_PROMOTE_CAP = 6
try:
    FUNNEL_THEME_RADAR_BONUS_MAX = max(float(os.getenv("FUNNEL_THEME_RADAR_BONUS_MAX", "18")), 0.0)
except Exception:
    logger.debug("FUNNEL_THEME_RADAR_BONUS_MAX parse failed, using default", exc_info=True)
    FUNNEL_THEME_RADAR_BONUS_MAX = 18.0
try:
    FUNNEL_THEME_RADAR_MAX_AGE_DAYS = max(int(float(os.getenv("FUNNEL_THEME_RADAR_MAX_AGE_DAYS", "14"))), 0)
except Exception:
    logger.debug("FUNNEL_THEME_RADAR_MAX_AGE_DAYS parse failed, using default", exc_info=True)
    FUNNEL_THEME_RADAR_MAX_AGE_DAYS = 14
FUNNEL_STRATEGIC_L2_BYPASS_ENABLED = os.getenv("FUNNEL_STRATEGIC_L2_BYPASS_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
try:
    FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP = max(int(float(os.getenv("FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP", "12"))), 0)
except Exception:
    logger.debug("FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP parse failed, using default", exc_info=True)
    FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP = 12
try:
    FUNNEL_STRATEGIC_L2_BYPASS_MIN_THEME_SCORE = max(
        float(os.getenv("FUNNEL_STRATEGIC_L2_BYPASS_MIN_THEME_SCORE", "0.45")),
        0.0,
    )
except Exception:
    logger.debug("FUNNEL_STRATEGIC_L2_BYPASS_MIN_THEME_SCORE parse failed, using default", exc_info=True)
    FUNNEL_STRATEGIC_L2_BYPASS_MIN_THEME_SCORE = 0.45
try:
    FUNNEL_STRATEGIC_L2_BYPASS_MIN_STOCK_SCORE = max(
        float(os.getenv("FUNNEL_STRATEGIC_L2_BYPASS_MIN_STOCK_SCORE", "0.55")),
        0.0,
    )
except Exception:
    logger.debug("FUNNEL_STRATEGIC_L2_BYPASS_MIN_STOCK_SCORE parse failed, using default", exc_info=True)
    FUNNEL_STRATEGIC_L2_BYPASS_MIN_STOCK_SCORE = 0.55

FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_ENABLED = os.getenv(
    "FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_ENABLED", "1"
).strip().lower() in {"1", "true", "yes", "on"}
try:
    FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_MIN_SCORE = max(
        float(os.getenv("FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_MIN_SCORE", "55")),
        0.0,
    )
except Exception:
    logger.debug("FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_MIN_SCORE parse failed, using default", exc_info=True)
    FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_MIN_SCORE = 55.0

# TradingAgents 精选池注入：逗号/分号分隔的股票代码
FUNNEL_EXTRA_SYMBOLS_RAW = os.getenv("FUNNEL_EXTRA_SYMBOLS", "").strip()
FUNNEL_EXTRA_ENABLED = bool(FUNNEL_EXTRA_SYMBOLS_RAW)
FUNNEL_EXTRA_BONUS_SCORE = float(os.getenv("FUNNEL_EXTRA_BONUS_SCORE", "15.0"))
FUNNEL_EXTRA_TRACK_TTL_DAYS = int(os.getenv("FUNNEL_EXTRA_TRACK_TTL_DAYS", "5"))
FUNNEL_EXTRA_MAX_WATCH = int(os.getenv("FUNNEL_EXTRA_MAX_WATCH", "30"))


def _resolve_funnel_end_calendar_day() -> date:
    """Resolve the funnel end date, allowing replay jobs to pin a historical day."""
    raw = os.getenv("END_CALENDAR_DAY", "").strip()
    if raw:
        try:
            return pd.to_datetime(raw).date()
        except Exception as e:
            logger.warning("END_CALENDAR_DAY=%r 解析失败，回退自动日期: %s", raw, e)
    return resolve_end_calendar_day()


from tools.data_fetcher import (
    fetch_all_ohlcv,
)
from tools.data_fetcher import (
    latest_trade_date_from_hist as _latest_trade_date_from_hist,
)
from tools.funnel_config import (
    apply_funnel_cfg_overrides as _apply_funnel_cfg_overrides,
)
from tools.market_regime import (
    analyze_benchmark_and_tune_cfg as _analyze_benchmark_and_tune_cfg,
)
from tools.market_regime import (
    calc_market_breadth as _calc_market_breadth,
)
from tools.symbol_pool import (
    _stock_name_map,
)
from tools.symbol_pool import (
    resolve_symbol_pool_from_env as _resolve_symbol_pool_from_env,
)


def _dump_full_fetch_snapshot(
    df_map: dict[str, pd.DataFrame],
    all_symbols: list[str],
    window,
    fetch_stats: dict,
    bench_df: pd.DataFrame | None = None,
    smallcap_df: pd.DataFrame | None = None,
) -> str | None:
    """
    将本轮全量拉取结果落盘，便于后续离线复现和自测。
    导出内容：
    - hist_full.csv.gz: 全量历史（日线）明细（含 symbol 列）
    - latest_quotes.csv: 每只股票最新一条记录
    - fetch_status.csv: 每只股票拉取状态
    - benchmark_main.csv / benchmark_smallcap.csv: 基准指数日线
    - metadata.json: 运行元信息
    """
    if not FUNNEL_EXPORT_FULL_FETCH:
        return None
    if not all_symbols:
        return None

    try:
        base_dir = Path(FUNNEL_EXPORT_DIR)
        base_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S")
        run_dir = base_dir / f"full_fetch_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)

        frames: list[pd.DataFrame] = []
        status_rows: list[dict] = []
        for symbol in all_symbols:
            df = df_map.get(symbol)
            if df is None or df.empty:
                status_rows.append(
                    {
                        "symbol": symbol,
                        "fetched": 0,
                        "rows": 0,
                        "latest_trade_date": "",
                    }
                )
                continue

            one = df.copy()
            one.insert(0, "symbol", symbol)
            if "date" in one.columns:
                one["date"] = pd.to_datetime(one["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            frames.append(one)
            latest_trade_date = _latest_trade_date_from_hist(df)
            status_rows.append(
                {
                    "symbol": symbol,
                    "fetched": 1,
                    "rows": int(len(df)),
                    "latest_trade_date": latest_trade_date.isoformat() if latest_trade_date else "",
                }
            )

        full_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        full_path = run_dir / "hist_full.csv.gz"
        full_df.to_csv(full_path, index=False, compression="gzip")

        if not full_df.empty and {"symbol", "date"}.issubset(full_df.columns):
            latest_df = (
                full_df.sort_values(["symbol", "date"])
                .groupby("symbol", as_index=False)
                .tail(1)
                .sort_values("symbol")
                .reset_index(drop=True)
            )
        else:
            latest_df = pd.DataFrame(columns=["symbol"])
        latest_df.to_csv(run_dir / "latest_quotes.csv", index=False)

        status_df = pd.DataFrame(status_rows).sort_values("symbol").reset_index(drop=True)
        status_df.to_csv(run_dir / "fetch_status.csv", index=False)

        def _dump_benchmark(df_src: pd.DataFrame | None, filename: str) -> bool:
            if df_src is None or df_src.empty:
                return False
            cols = [c for c in ["date", "open", "high", "low", "close", "volume", "pct_chg"] if c in df_src.columns]
            if not cols:
                return False
            one = df_src[cols].copy()
            if "date" in one.columns:
                one["date"] = pd.to_datetime(one["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            one.to_csv(run_dir / filename, index=False)
            return True

        has_bench_main = _dump_benchmark(bench_df, "benchmark_main.csv")
        has_bench_smallcap = _dump_benchmark(smallcap_df, "benchmark_smallcap.csv")

        metadata = {
            "generated_at": datetime.now(CN_TZ).isoformat(),
            "export_dir": str(run_dir),
            "window_start_trade_date": window.start_trade_date.isoformat(),
            "window_end_trade_date": window.end_trade_date.isoformat(),
            "symbols_total": int(len(all_symbols)),
            "symbols_fetched": int(sum(1 for s in status_rows if s["fetched"] == 1)),
            "rows_total": int(len(full_df)),
            "fetch_stats": fetch_stats,
            "has_benchmark_main": has_bench_main,
            "has_benchmark_smallcap": has_bench_smallcap,
        }
        with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        with open(base_dir / "latest_run.txt", "w", encoding="utf-8") as f:
            f.write(str(run_dir) + "\n")

        print(
            "[funnel] 全量快照已落盘: "
            f"{run_dir} (symbols={metadata['symbols_fetched']}/{metadata['symbols_total']}, "
            f"rows={metadata['rows_total']})"
        )
        return str(run_dir)
    except Exception as e:
        logger.error("全量快照落盘失败: %s", e, exc_info=True)
        return None


_ETF_UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "data" / "market_universes" / "etf_cn.txt"


def _load_etf_universe() -> tuple[list[str], dict[str, str]]:
    """读取 ETF 板块增强池，返回 (etf_codes, {code: sector_tag})。"""
    if not _ETF_UNIVERSE_PATH.is_file():
        return [], {}
    codes: list[str] = []
    sector_map: dict[str, str] = {}
    for line in _ETF_UNIVERSE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        code, tag = parts[0].strip(), parts[1].strip()
        if len(code) == 6 and code.isdigit() and tag:
            codes.append(code)
            sector_map[code] = tag
    return codes, sector_map


def _fetch_etf_ohlcv(
    etf_symbols: list[str],
    window,
    *,
    batch_size: int = 50,
    direct_source: bool = False,
) -> dict[str, pd.DataFrame]:
    """拉取 ETF 行情，无数据源时优雅降级。"""
    if not etf_symbols:
        return {}
    has_tickflow = bool(os.getenv("TICKFLOW_API_KEY", "").strip())
    has_tushare = bool(os.getenv("TUSHARE_TOKEN", "").strip())
    if not has_tickflow and not has_tushare:
        print(f"[funnel] ⚠️ ETF 板块增强需要数据源，跳过。购买 TickFlow：{TICKFLOW_UPGRADE_URL}")
        return {}
    df_map, _ = fetch_all_ohlcv(
        symbols=etf_symbols,
        window=window,
        batch_size=batch_size,
        max_workers=4,
        batch_timeout=120,
        batch_sleep=1,
        executor_mode="thread",
        direct_source=direct_source,
    )
    if not df_map:
        print("[funnel] ETF 行情拉取失败，跳过板块增强")
    return df_map


def _build_etf_funnel_config(base_cfg: FunnelConfig) -> FunnelConfig:
    """ETF 专属漏斗配置：波动率低，放宽触发门槛。"""
    cfg = FunnelConfig(trading_days=base_cfg.trading_days)
    cfg.require_cn_main_or_chinext = False
    cfg.min_market_cap_yi = 0.0
    cfg.min_avg_amount_wan = 50.0
    cfg.enable_rs_filter = False
    cfg.enable_rps_filter = False
    cfg.enable_rs_divergence_channel = False
    cfg.require_bench_latest_alignment = False
    cfg.sos_pct_min = 3.5
    cfg.sos_vol_ratio = 2.0
    cfg.spring_vol_ratio = 1.0
    cfg.evr_min_turnover = 0.3
    cfg.evr_max_rise = 2.0
    return cfg


def _etf_display_name(code: str, sector_map: dict[str, str]) -> str:
    tag = str(sector_map.get(code, "") or "").strip()
    if not tag:
        return code
    if tag.upper().endswith("ETF") or tag.endswith("基金"):
        return tag
    return f"{tag}ETF"


def _latest_volume_ratio(df: pd.DataFrame) -> float | None:
    if df is None or df.empty or "volume" not in df.columns:
        return None
    volume = pd.to_numeric(df["volume"], errors="coerce")
    vol_ma20 = volume.rolling(20, min_periods=5).mean()
    latest = volume.dropna()
    ma_latest = vol_ma20.dropna()
    if latest.empty or ma_latest.empty:
        return None
    base = float(ma_latest.iloc[-1])
    if base <= 0:
        return None
    return float(latest.iloc[-1]) / base


def _rank_etf_candidates(
    l2_passed: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    channel_map: dict[str, str],
) -> list[dict]:
    rows: list[dict] = []
    for code in l2_passed:
        df = df_map.get(code)
        if df is None or df.empty or "close" not in df.columns:
            continue
        s = df.sort_values("date") if "date" in df.columns else df
        close = pd.to_numeric(s["close"], errors="coerce")
        ret3 = calc_close_return_pct(close, 3)
        ret5 = calc_close_return_pct(close, 5)
        ret20 = calc_close_return_pct(close, 20)
        vol_ratio = _latest_volume_ratio(s)
        channel = str(channel_map.get(code, "") or "").strip()
        channel_bonus = 3.0 if "主升" in channel or "点火" in channel else 0.0
        score = (
            max(ret20 or 0.0, -10.0) * 0.35
            + max(ret5 or 0.0, -5.0) * 0.75
            + max(ret3 or 0.0, -3.0) * 1.1
            + min(max(vol_ratio or 1.0, 0.0), 3.0) * 2.0
            + channel_bonus
        )
        rows.append(
            {
                "code": code,
                "name": _etf_display_name(code, sector_map),
                "sector": str(sector_map.get(code, "") or ""),
                "channel": channel,
                "ret3": ret3,
                "ret5": ret5,
                "ret20": ret20,
                "vol_ratio": vol_ratio,
                "score": score,
            }
        )
    rows.sort(key=lambda row: float(row.get("score", 0.0) or 0.0), reverse=True)
    return rows


def _load_benchmark_indices(start_s: str, end_s: str):
    """加载大盘和小盘基准指数，失败时优雅降级。"""
    bench_df = smallcap_df = None
    try:
        bench_df = fetch_index_hist("000001", start_s, end_s)
        print("[funnel] 大盘基准加载成功")
    except Exception as e:
        logger.error("大盘基准加载失败: %s", e, exc_info=True)
    try:
        smallcap_df = fetch_index_hist(SMALLCAP_BENCH_CODE, start_s, end_s)
        print(f"[funnel] 小盘基准加载成功: {SMALLCAP_BENCH_CODE}")
    except Exception as e:
        logger.error("小盘基准加载失败 %s: %s", SMALLCAP_BENCH_CODE, e, exc_info=True)
    return bench_df, smallcap_df


def _run_etf_enhancement(
    base_cfg: FunnelConfig,
    window,
    bench_df: pd.DataFrame | None,
    sector_map: dict[str, str],
    all_df_map: dict[str, pd.DataFrame],
    *,
    direct_source: bool = False,
) -> tuple[list[str], dict[str, str], dict[str, pd.DataFrame], list[str], list[dict]]:
    """加载 ETF 并跑 L1/L2，过 L2 的 ETF 注入 sector_map 和 all_df_map。"""
    etf_symbols, etf_sector_map = _load_etf_universe()
    etf_df_map = _fetch_etf_ohlcv(etf_symbols, window, direct_source=direct_source)
    etf_l2_passed: list[str] = []
    etf_candidates: list[dict] = []
    if etf_df_map:
        etf_cfg = _build_etf_funnel_config(base_cfg)
        etf_l1 = layer1_filter(list(etf_df_map.keys()), {}, {}, etf_df_map, etf_cfg)
        if etf_l1:
            etf_l2, etf_channel_map, _ = layer2_strength_detailed(
                etf_l1,
                etf_df_map,
                bench_df,
                etf_cfg,
                rps_universe=etf_l1,
            )
            etf_l2_passed = etf_l2
            etf_candidates = _rank_etf_candidates(etf_l2_passed, etf_df_map, etf_sector_map, etf_channel_map)
            sector_map.update(etf_sector_map)
            all_df_map.update(etf_df_map)
        print(f"[funnel] ETF板块增强: fetched={len(etf_df_map)}, L1={len(etf_l1)}, L2={len(etf_l2_passed)}")
    else:
        print("[funnel] ETF板块增强: 跳过")
    return etf_symbols, etf_sector_map, etf_df_map, etf_l2_passed, etf_candidates


def _etf_metrics(syms, df_map, l2_passed, sector_map, candidates=None) -> dict:
    return {
        "pool": len(syms),
        "fetched": len(df_map),
        "l2_passed": len(l2_passed),
        "strong_candidates": len(candidates or []),
        "boosted_sectors": sorted({sector_map.get(s, "") for s in l2_passed} - {""}),
    }


def _fmt_pct(value) -> str:
    try:
        return f"{float(value):+.1f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_ratio(value) -> str:
    try:
        return f"{float(value):.2f}x"
    except (TypeError, ValueError):
        return "-"


def _append_etf_section(lines: list[str], etf_metrics: dict, etf_candidates: list[dict]) -> None:
    if not etf_metrics and not etf_candidates:
        return
    pool = int(etf_metrics.get("pool", 0) or 0)
    fetched = int(etf_metrics.get("fetched", 0) or 0)
    l2_passed = int(etf_metrics.get("l2_passed", 0) or 0)
    lines.append(f"**ETF强势池**: 池{pool} → 拉取{fetched} → L2强势{l2_passed}")
    if not etf_candidates:
        return

    display = etf_candidates if FUNNEL_ETF_DISPLAY_LIMIT <= 0 else etf_candidates[:FUNNEL_ETF_DISPLAY_LIMIT]
    lines.append(f"**【📈 强势ETF】{len(etf_candidates)} 只**")
    for row in display:
        channel = str(row.get("channel", "") or "").replace("通道", "")
        parts = [
            f"3日{_fmt_pct(row.get('ret3'))}",
            f"20日{_fmt_pct(row.get('ret20'))}",
            f"量{_fmt_ratio(row.get('vol_ratio'))}",
        ]
        if channel:
            parts.append(channel)
        lines.append(
            f"  {row.get('code')} {row.get('name')}  {float(row.get('score', 0.0) or 0.0):.2f}  {' | '.join(parts)}"
        )
    omitted = len(etf_candidates) - len(display)
    if omitted > 0:
        lines.append(f"  ... 另 {omitted} 只略")


def _merge_trigger_maps(*trigger_maps: dict[str, list[tuple[str, float]]]) -> dict[str, list[tuple[str, float]]]:
    merged: dict[str, list[tuple[str, float]]] = {key: [] for key in TRIGGER_LABELS}
    seen: set[tuple[str, str]] = set()
    for source in trigger_maps:
        for key, hits in (source or {}).items():
            bucket = merged.setdefault(str(key), [])
            for code, score in hits or []:
                code_s = str(code).strip()
                dedupe_key = (str(key), code_s)
                if not code_s or dedupe_key in seen:
                    continue
                bucket.append((code_s, float(score or 0.0)))
                seen.add(dedupe_key)
    return merged


def _score_star(score: float) -> str:
    if score >= 10:
        return "★★"
    if score >= 5:
        return "★ "
    return "  "


def _append_formal_l4_sections(
    lines: list[str],
    formal_codes: list[str],
    selected_codes: list[str],
    name_map: dict[str, str],
    code_to_trigger_keys: dict[str, list[str]],
    display_score: Callable[[str], float],
    theme_badge_map: dict[str, str] | None = None,
) -> None:
    selected_set = set(selected_codes)
    badge_map = theme_badge_map or {}

    def _append_row(code: str, extra: str = "") -> None:
        score = float(display_score(code))
        ai_mark = "  →AI" if code in selected_set else ""
        badge = f"  {badge_map[code]}" if code in badge_map else ""
        lines.append(f"{_score_star(score)} {code} {name_map.get(code, code)}  {score:.2f}{ai_mark}{extra}{badge}")

    multi_signal = [c for c in formal_codes if len(code_to_trigger_keys.get(c, [])) > 1]
    if multi_signal:
        lines.append(f"**【🔥 多信号共振】{len(multi_signal)} 只**")
        for code in sorted(multi_signal, key=lambda c: -float(display_score(c))):
            short = "+".join(TRIGGER_SHORT_LABELS.get(k, k) for k in code_to_trigger_keys.get(code, []))
            _append_row(code, f"  {short}")
        lines.append("")

    multi_signal_set = set(multi_signal)
    single_signal_codes = [c for c in formal_codes if c not in multi_signal_set and code_to_trigger_keys.get(c)]
    code_primary_key = {code: code_to_trigger_keys.get(code, [""])[0] for code in single_signal_codes}
    for group_key in TRIGGER_GROUP_ORDER:
        group_codes = [c for c in single_signal_codes if code_primary_key.get(c) == group_key]
        if not group_codes:
            continue
        lines.append(f"**【{TRIGGER_GROUP_TITLES.get(group_key, group_key)}】{len(group_codes)} 只**")
        for code in sorted(group_codes, key=lambda c: -float(display_score(c))):
            _append_row(code)
        lines.append("")


def _is_accum_trigger(keys: list[str]) -> bool:
    key_set = {str(k).strip().lower() for k in keys}
    return bool(key_set & {"spring", "lps"}) and not bool(key_set & {"sos", "evr", "compression"})


def _split_selected_tracks(
    selected_codes: list[str],
    code_to_trigger_keys: dict[str, list[str]],
) -> tuple[list[str], list[str]]:
    trend_selected: list[str] = []
    accum_selected: list[str] = []
    for code in selected_codes:
        if _is_accum_trigger(code_to_trigger_keys.get(code, [])):
            accum_selected.append(code)
        else:
            trend_selected.append(code)
    return trend_selected, accum_selected


def _rank_l2_bypass_pool(l2_bypass_pool: list[str], code_to_total_score: dict[str, float]) -> list[str]:
    clean_pool = {str(code).strip() for code in l2_bypass_pool if str(code).strip()}
    return sorted(clean_pool, key=lambda c: (-code_to_total_score.get(c, 0.0), c))


def _promote_l2_bypass_for_ai(
    selected_for_ai: list[str],
    trend_selected: list[str],
    accum_selected: list[str],
    l2_bypass_pool: list[str],
    code_to_total_score: dict[str, float],
    code_to_trigger_keys: dict[str, list[str]],
    score_map: dict[str, float],
    *,
    enabled: bool | None = None,
    cap: int | None = None,
    accum_codes: set[str] | None = None,
) -> int:
    if not (FUNNEL_L2_BYPASS_AI_ENABLED if enabled is None else enabled) or not l2_bypass_pool:
        return 0
    ranked = _rank_l2_bypass_pool(l2_bypass_pool, code_to_total_score)
    budget = FUNNEL_L2_BYPASS_AI_CAP if cap is None else cap
    ranked = ranked if budget <= 0 else ranked[:budget]
    selected_seen = set(selected_for_ai)
    track_seen = set(trend_selected) | set(accum_selected)
    accum_set = accum_codes or set()
    added = 0
    for code in ranked:
        if code not in selected_seen:
            selected_for_ai.append(code)
            selected_seen.add(code)
            added += 1
        score_map.setdefault(code, float(code_to_total_score.get(code, 0.0) or 0.0))
        if code in track_seen:
            continue
        if code in accum_set or _is_accum_trigger(code_to_trigger_keys.get(code, [])):
            accum_selected.append(code)
        else:
            trend_selected.append(code)
        track_seen.add(code)
    return added


def _load_dynamic_policy_context(regime: str, benchmark_context: dict) -> dict:
    mode = dynamic_policy_mode()
    if mode == "off":
        return {"mode": mode, "health": [], "registry": [], "weights": {}, "policy": None}
    try:
        health_rows = load_signal_health_snapshot(market="cn")
        registry_rows = load_signal_registry(market="cn")
    except Exception as exc:
        logger.warning("动态策略上下文加载失败，降级为静态: %s", exc)
        return {"mode": "off", "health": [], "registry": [], "weights": {}, "policy": None}
    weights = build_signal_weight_map(health_rows, registry_rows, regime=regime)
    base_policy = resolve_ai_candidate_policy(regime)
    policy = resolve_dynamic_candidate_policy(
        base_policy,
        weights,
        breadth=(benchmark_context.get("breadth") or {}),
    )
    if health_rows or registry_rows:
        print(
            "[funnel] 动态策略上下文: "
            f"mode={mode}, weights={weights or {}}, "
            f"TrendWeight={policy.get('trend_health_weight', 1)}, "
            f"AccumWeight={policy.get('accum_health_weight', 1)}"
        )
    return {"mode": mode, "health": health_rows, "registry": registry_rows, "weights": weights, "policy": policy}


def _attach_shadow_policy(ai_policy: dict, dynamic_ctx: dict) -> None:
    if str(dynamic_ctx.get("mode") or "off") != "shadow" or not dynamic_ctx.get("policy"):
        return
    shadow_policy = dynamic_ctx["policy"]
    ai_policy["_dynamic_mode"] = "shadow"
    ai_policy["_shadow_policy"] = shadow_policy
    ai_policy["_signal_weights"] = dynamic_ctx.get("weights") or {}
    ai_policy["_registry_rows"] = dynamic_ctx.get("registry") or []
    ai_policy["_health_rows"] = dynamic_ctx.get("health") or []
    print(
        "[funnel] 动态策略shadow: "
        f"base Trend={ai_policy['trend_quota']}, Accum={ai_policy['accum_quota']} -> "
        f"shadow Trend={shadow_policy['trend_quota']}, Accum={shadow_policy['accum_quota']}"
    )


def _candidate_result(metrics: dict, triggers: dict[str, list[tuple[str, float]]]) -> FunnelResult:
    return FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=metrics.get("layer3_symbols", []) or [],
        top_sectors=[],
        triggers=triggers,
        stage_map=metrics.get("accum_stage_map", {}) or {},
        markup_symbols=metrics.get("markup_symbols", []) or [],
        exit_signals=metrics.get("exit_signals", {}) or {},
        channel_map=metrics.get("layer2_channel_map", {}) or {},
    )


def _public_policy(policy: dict) -> dict:
    return {k: v for k, v in policy.items() if not str(k).startswith("_")}


def _selection_diff(base_selected: list[str], shadow_selected: list[str]) -> tuple[list[str], list[str]]:
    base_set = set(base_selected)
    shadow_set = set(shadow_selected)
    return ([c for c in shadow_selected if c not in base_set], [c for c in base_selected if c not in shadow_set])


def _allocate_candidates_for_ai(
    metrics: dict,
    triggers: dict[str, list[tuple[str, float]]],
    l3_ranked_symbols: list[str],
    regime: str,
    sector_map: dict[str, str],
    benchmark_context: dict,
) -> tuple[list[str], list[str], dict[str, float], dict]:
    dynamic_ctx = _load_dynamic_policy_context(str(regime), benchmark_context)
    dynamic_mode = str(dynamic_ctx.get("mode") or "off")
    allocation_triggers = triggers
    if dynamic_mode == "on":
        allocation_triggers = filter_triggers_by_registry(triggers, dynamic_ctx.get("registry", []) or [])
    mock_result = _candidate_result(metrics, allocation_triggers)
    alloc_started = time.monotonic()
    dynamic_policy = dynamic_ctx.get("policy") if dynamic_mode == "on" else None
    trend_selected, accum_selected, score_map = allocate_ai_candidates(
        mock_result,
        l3_ranked_symbols,
        regime,
        sector_map=sector_map,
        max_per_sector=2,
        policy_override=dynamic_policy,
        signal_weight_map=(dynamic_ctx.get("weights") or {}) if dynamic_mode == "on" else None,
    )
    ai_policy = dynamic_policy or resolve_ai_candidate_policy(regime)
    _attach_shadow_policy(ai_policy, dynamic_ctx)
    alloc_elapsed = time.monotonic() - alloc_started
    print(
        f"[funnel] AI候选分配完成: trend={len(trend_selected)}, accum={len(accum_selected)}, "
        f"elapsed={alloc_elapsed:.3f}s"
    )
    return trend_selected, accum_selected, score_map, ai_policy


def _shadow_selected_codes(
    metrics: dict,
    triggers: dict[str, list[tuple[str, float]]],
    l3_ranked_symbols: list[str],
    regime: str,
    sector_map: dict[str, str],
    ai_policy: dict,
) -> tuple[list[str], list[str], dict[str, float]]:
    shadow_triggers = filter_triggers_by_registry(triggers, ai_policy.get("_registry_rows", []) or [])
    trend, accum, score_map = allocate_ai_candidates(
        _candidate_result(metrics, shadow_triggers),
        l3_ranked_symbols,
        regime,
        sector_map=sector_map,
        max_per_sector=2,
        policy_override=ai_policy.get("_shadow_policy"),
        signal_weight_map=ai_policy.get("_signal_weights") or {},
    )
    return trend, accum, score_map


def _maybe_persist_policy_shadow_run(
    *,
    ai_policy: dict,
    metrics: dict,
    triggers: dict[str, list[tuple[str, float]]],
    selected_for_ai: list[str],
    l3_ranked_symbols: list[str],
    regime: str,
    sector_map: dict[str, str],
) -> dict:
    if ai_policy.get("_dynamic_mode") != "shadow" or not ai_policy.get("_shadow_policy"):
        return {}
    shadow_trend, shadow_accum, _score_map = _shadow_selected_codes(
        metrics,
        triggers,
        l3_ranked_symbols,
        regime,
        sector_map,
        ai_policy,
    )
    shadow_selected = shadow_trend + shadow_accum
    diff_added, diff_removed = _selection_diff(selected_for_ai, shadow_selected)
    row = {
        "market": "cn",
        "trade_date": str(metrics.get("end_trade_date") or date.today().isoformat()),
        "regime": str(regime or "NEUTRAL").strip().upper() or "NEUTRAL",
        "base_policy": _public_policy(ai_policy),
        "shadow_policy": _public_policy(ai_policy.get("_shadow_policy") or {}),
        "signal_weights": ai_policy.get("_signal_weights") or {},
        "base_selected": selected_for_ai,
        "shadow_selected": shadow_selected,
        "diff_added": diff_added,
        "diff_removed": diff_removed,
        "registry_snapshot": ai_policy.get("_registry_rows") or [],
        "health_snapshot": ai_policy.get("_health_rows") or [],
        "updated_at": datetime.now(CN_TZ).isoformat(),
    }
    written = upsert_policy_shadow_run(row)
    print(
        "[funnel] 动态策略shadow已写入 signal_policy_shadow_runs: "
        f"written={written}, added={len(diff_added)}, removed={len(diff_removed)}"
    )
    return {
        "shadow_table": "signal_policy_shadow_runs",
        "shadow_written": written,
        "shadow_added_count": len(diff_added),
        "shadow_removed_count": len(diff_removed),
    }


def _load_theme_radar_history() -> dict:
    try:
        from integrations.supabase_concept_heat import load_concept_heat_history_from_supabase

        history = load_concept_heat_history_from_supabase()
        if history:
            return history
    except Exception as exc:
        logger.debug("theme radar supabase history unavailable: %s", exc)
    try:
        if _CONCEPT_HEAT_HISTORY.exists():
            with open(_CONCEPT_HEAT_HISTORY, encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.debug("theme radar local history unavailable: %s", exc)
    return {}


def _safe_build_theme_radar(
    *,
    trade_date: str,
    concept_heat: list[dict],
    concept_map: dict[str, list[str]],
    sector_map: dict[str, str],
    df_map: dict[str, pd.DataFrame],
    name_map: dict[str, str],
) -> dict:
    if not FUNNEL_THEME_RADAR_ENABLED:
        return {"trade_date": trade_date, "themes": [], "strategic_candidates": []}
    try:
        return build_theme_radar_snapshot(
            trade_date=trade_date,
            concept_heat=concept_heat,
            concept_history=_load_theme_radar_history(),
            concept_map=concept_map,
            sector_map=sector_map,
            df_map=df_map,
            name_map=name_map,
        )
    except Exception as exc:
        logger.warning("theme radar build failed: %s", exc)
        return {"trade_date": trade_date, "themes": [], "strategic_candidates": []}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _theme_snapshot_age_days(snapshot: dict, trade_date: str) -> int:
    try:
        snapshot_date = pd.to_datetime(str(snapshot.get("trade_date") or "")).date()
        current_date = pd.to_datetime(str(trade_date)).date()
        return abs((current_date - snapshot_date).days)
    except Exception:
        return FUNNEL_THEME_RADAR_MAX_AGE_DAYS + 1


def _has_theme_radar_payload(snapshot: dict | None) -> bool:
    if not snapshot:
        return False
    return bool(snapshot.get("themes") or snapshot.get("strategic_candidates"))


def _resolve_linked_theme_radar(current_snapshot: dict, trade_date: str) -> tuple[dict, str]:
    if not FUNNEL_THEME_RADAR_ENABLED:
        return {"trade_date": trade_date, "themes": [], "strategic_candidates": []}, "disabled"
    if not FUNNEL_THEME_RADAR_LINK_ENABLED:
        return current_snapshot, "current"
    try:
        from integrations.theme_radar_storage import load_latest_theme_radar_snapshot

        persisted = load_latest_theme_radar_snapshot()
    except Exception as exc:
        logger.debug("theme radar persisted snapshot unavailable: %s", exc)
        persisted = None
    if _has_theme_radar_payload(persisted):
        age_days = _theme_snapshot_age_days(persisted or {}, trade_date)
        if age_days <= FUNNEL_THEME_RADAR_MAX_AGE_DAYS:
            return persisted or current_snapshot, "persisted"
    return current_snapshot, "current"


def _theme_candidate_map(snapshot: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in snapshot.get("strategic_candidates") or []:
        code = str(item.get("code", "") or "").strip()
        if code:
            out[code] = item
    return out


def _theme_badge_map(candidate_map: dict[str, dict]) -> dict[str, str]:
    badges: dict[str, str] = {}
    for code, item in candidate_map.items():
        theme = str(item.get("theme", "") or "").strip()
        theme_score = _safe_float(item.get("theme_score"))
        if theme:
            badges[code] = f"战略主线:{theme}({theme_score:.2f})"
    return badges


def _theme_bonus_map(candidate_map: dict[str, dict]) -> dict[str, float]:
    bonuses: dict[str, float] = {}
    if FUNNEL_THEME_RADAR_BONUS_MAX <= 0:
        return bonuses
    for code, item in candidate_map.items():
        theme_score = _safe_float(item.get("theme_score"))
        stock_score = _safe_float(item.get("stock_score"))
        score = max(min(0.55 * theme_score + 0.45 * stock_score, 1.0), 0.0)
        if score > 0:
            bonuses[code] = round(score * FUNNEL_THEME_RADAR_BONUS_MAX, 4)
    return bonuses


def _append_theme_reasons(code_to_reasons: dict[str, list[str]], badge_map: dict[str, str]) -> None:
    for code, badge in badge_map.items():
        if code in code_to_reasons and badge not in code_to_reasons[code]:
            code_to_reasons[code].append(badge)


def _apply_theme_bonus_to_scores(score_map: dict[str, float], bonus_map: dict[str, float]) -> None:
    for code, bonus in bonus_map.items():
        if code in score_map:
            score_map[code] = float(score_map.get(code, 0.0) or 0.0) + float(bonus)


def _promote_theme_l4_for_ai(
    selected_for_ai: list[str],
    trend_selected: list[str],
    accum_selected: list[str],
    formal_hit_set: set[str],
    theme_bonus_map: dict[str, float],
    code_to_total_score: dict[str, float],
    code_to_trigger_keys: dict[str, list[str]],
    score_map: dict[str, float],
) -> int:
    ranked = [code for code in formal_hit_set if code in theme_bonus_map]
    ranked.sort(key=lambda c: (-float(code_to_total_score.get(c, 0.0) or 0.0), c))
    ranked = ranked if FUNNEL_THEME_RADAR_PROMOTE_CAP <= 0 else ranked[:FUNNEL_THEME_RADAR_PROMOTE_CAP]
    selected_seen = set(selected_for_ai)
    track_seen = set(trend_selected) | set(accum_selected)
    added = 0
    for code in ranked:
        score_map.setdefault(code, float(code_to_total_score.get(code, 0.0) or 0.0))
        if code not in selected_seen:
            selected_for_ai.append(code)
            selected_seen.add(code)
            added += 1
        if code in track_seen:
            continue
        if _is_accum_trigger(code_to_trigger_keys.get(code, [])):
            accum_selected.append(code)
        else:
            trend_selected.append(code)
        track_seen.add(code)
    return added


def _rerank_selected_codes(codes: list[str], score_map: dict[str, float]) -> list[str]:
    seen: set[str] = set()
    deduped = []
    for code in codes:
        if code not in seen:
            deduped.append(code)
            seen.add(code)
    return sorted(deduped, key=lambda c: (-float(score_map.get(c, 0.0) or 0.0), c))


def _theme_report_fields(code: str, candidate_map: dict[str, dict], bonus_map: dict[str, float]) -> dict:
    item = candidate_map.get(code) or {}
    return {
        "strategic_theme": str(item.get("theme", "") or "").strip(),
        "strategic_theme_score": _safe_float(item.get("theme_score")),
        "strategic_stock_score": _safe_float(item.get("stock_score")),
        "strategic_theme_state": str(item.get("state", "") or "").strip(),
        "strategic_theme_bonus": _safe_float(bonus_map.get(code)),
    }


def _signal_report_fields(
    code: str,
    trigger_key_map: dict[str, list[str]],
    track: str,
    regime: str,
    trigger_score: float,
) -> dict:
    signal_types: list[str] = []
    for key in trigger_key_map.get(code, []) or []:
        signal = str(key or "").strip()
        if signal and signal not in signal_types:
            signal_types.append(signal)
    return {
        "primary_signal": signal_types[0] if signal_types else "",
        "signal_types": signal_types,
        "signal_track": str(track or "").strip(),
        "market_regime": str(regime or "NEUTRAL").strip().upper() or "NEUTRAL",
        "trigger_score": _safe_float(trigger_score),
    }


def _candidate_reason_text(code: str, code_to_reasons: dict[str, list[str]], badge_map: dict[str, str]) -> str:
    reasons = list(code_to_reasons.get(code, []) or [])
    badge = badge_map.get(code, "")
    if badge and badge not in reasons:
        reasons.append(badge)
    return "、".join(reasons) or "威科夫候选"


def _strategic_bypass_seed_codes(
    l1_symbols: list[str],
    l2_symbols: list[str],
    candidate_map: dict[str, dict],
) -> list[str]:
    if not FUNNEL_STRATEGIC_L2_BYPASS_ENABLED:
        return []
    l2_set = {str(code).strip() for code in l2_symbols if str(code).strip()}
    seeds = []
    for code in l1_symbols:
        code_s = str(code).strip()
        item = candidate_map.get(code_s) or {}
        if code_s and code_s not in l2_set and _strategic_bypass_candidate_ok(item):
            seeds.append(code_s)
    return seeds


def _strategic_bypass_candidate_ok(item: dict) -> bool:
    state = str(item.get("state", "") or "").strip().lower()
    if state in {"decay", "overheated"}:
        return False
    return (
        _safe_float(item.get("theme_score")) >= FUNNEL_STRATEGIC_L2_BYPASS_MIN_THEME_SCORE
        and _safe_float(item.get("stock_score")) >= FUNNEL_STRATEGIC_L2_BYPASS_MIN_STOCK_SCORE
    )


def _trigger_hit_codes(trigger_map: dict[str, list[tuple[str, float]]]) -> set[str]:
    return {str(code).strip() for hits in (trigger_map or {}).values() for code, _ in hits if str(code).strip()}


def _strategic_stage_reason_map(stage_map: dict[str, str], markup_symbols: list[str]) -> dict[str, list[str]]:
    reasons = {str(code).strip(): ["战略阶段:Markup"] for code in markup_symbols if str(code).strip()}
    for code, stage in stage_map.items():
        code_s = str(code).strip()
        stage_s = str(stage or "").strip()
        if code_s and stage_s in {"Accum_B", "Accum_C"}:
            reasons.setdefault(code_s, []).append(f"战略阶段:{stage_s}")
    return reasons


def _fetch_rescue_klines(seed_codes: list[str]) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """为战略旁路候选批量拉取 60m/30m K线。失败时降级为空。"""
    empty: tuple[dict, dict] = ({}, {})
    if not FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_ENABLED or not seed_codes:
        return empty
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        return empty
    try:
        from integrations.tickflow_client import TickFlowClient, normalize_cn_symbol

        symbols = [normalize_cn_symbol(code) for code in seed_codes]
        symbols = [s for s in symbols if s]
        if not symbols:
            return empty
        client = TickFlowClient(api_key=api_key)
        df_60m_map = client.get_klines_batch(symbols, period="60m", count=100)
        df_30m_map = client.get_klines_batch(symbols, period="30m", count=100)
        return df_60m_map, df_30m_map
    except Exception as e:
        logger.warning("60m/30m rescue klines fetch failed: %s", e)
        return empty


def _rescue_structure_reason_map(
    seed_codes: list[str],
    df_60m_map: dict[str, pd.DataFrame],
    df_30m_map: dict[str, pd.DataFrame] | None = None,
) -> dict[str, list[str]]:
    """对有 60m 数据的候选逐只做结构救援分析。"""
    if not df_60m_map:
        return {}
    from core.intraday_analysis import analyze_rescue_structure
    from integrations.tickflow_client import normalize_cn_symbol

    threshold = FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_MIN_SCORE
    df_30m_map = df_30m_map or {}
    result: dict[str, list[str]] = {}
    for code in seed_codes:
        sym = normalize_cn_symbol(code)
        df_60 = df_60m_map.get(sym)
        if df_60 is None or getattr(df_60, "empty", True):
            continue
        df_30 = df_30m_map.get(sym)
        rescue = analyze_rescue_structure(df_60, df_30)
        if rescue.rescue_score >= threshold:
            result[code] = [f"60m结构救援({rescue.rescue_score:.0f}分)", *rescue.rescue_reasons]
    return result


def _build_strategic_l2_bypass(
    seed_codes: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    channel_map: dict[str, str],
    market_cap_map: dict[str, float],
) -> dict:
    if not seed_codes:
        return {"pool": [], "triggers": {}, "stage_map": {}, "markup_symbols": [], "reason_map": {}, "rescue_map": {}}
    trigger_map = layer4_triggers(seed_codes, df_map, cfg, channel_map=channel_map, market_cap_map=market_cap_map)
    stage_map = detect_accum_stage(seed_codes, df_map, cfg)
    markup_symbols = detect_markup_stage(seed_codes, df_map, cfg)
    reason_map = _strategic_stage_reason_map(stage_map, markup_symbols)
    df_60m_map, df_30m_map = _fetch_rescue_klines(seed_codes)
    rescue_reason_map = _rescue_structure_reason_map(seed_codes, df_60m_map, df_30m_map)
    for code, reasons in rescue_reason_map.items():
        reason_map.setdefault(code, []).extend(reasons)
    pool = sorted(_trigger_hit_codes(trigger_map) | set(reason_map))
    return {
        "pool": pool,
        "triggers": trigger_map,
        "stage_map": stage_map,
        "markup_symbols": markup_symbols,
        "reason_map": reason_map,
        "rescue_map": rescue_reason_map,
    }


def _append_extra_reasons(code_to_reasons: dict[str, list[str]], reason_map: dict[str, list[str]]) -> None:
    for code, reasons in reason_map.items():
        bucket = code_to_reasons.setdefault(code, [])
        for reason in reasons:
            if reason and reason not in bucket:
                bucket.append(reason)


def run_funnel_job(
    include_debug_context: bool = False,
    direct_source: bool = False,
) -> tuple[dict[str, list[tuple[str, float]]], dict]:
    """执行 Wyckoff Funnel，返回 (triggers, metrics)。"""
    cfg = FunnelConfig(trading_days=TRADING_DAYS)
    _apply_funnel_cfg_overrides(cfg)
    window = _resolve_trading_window(
        end_calendar_day=_resolve_funnel_end_calendar_day(),
        trading_days=TRADING_DAYS,
    )
    start_s = window.start_trade_date.strftime("%Y%m%d")
    end_s = window.end_trade_date.strftime("%Y%m%d")

    all_symbols, pool_name_map, pool_stats = _resolve_symbol_pool_from_env()
    main_items = [None] * int(pool_stats.get("pool_main", 0) or 0)
    chinext_items = [None] * int(pool_stats.get("pool_chinext", 0) or 0)
    merged_symbols = list(pool_name_map.keys())
    st_symbols = [None] * int(pool_stats.get("pool_st_excluded", 0) or 0)
    total_batches = (len(all_symbols) + BATCH_SIZE - 1) // BATCH_SIZE if all_symbols else 0
    print(
        "[funnel] 股票池统计: "
        f"mode={pool_stats.get('pool_mode')}, main={len(main_items)}, chinext={len(chinext_items)}, "
        f"merged={len(merged_symbols)}, st_excluded={len(st_symbols)}, "
        f"final={len(all_symbols)}, limit={pool_stats.get('pool_limit', 0)}, batches={total_batches} (batch_size={BATCH_SIZE})"
    )
    from cli.progress import report_progress

    report_progress("股票池加载", f"共{len(all_symbols)}只", 0.05)

    # 批量元数据
    print("[funnel] 加载行业映射...")
    try:
        sector_map = fetch_sector_map()
    except Exception as e:
        logger.warning("行业映射加载失败，降级为空映射: %s", e)
        sector_map = {}
    print("[funnel] 加载概念映射...")
    try:
        concept_map = fetch_concept_map()
    except Exception as e:
        logger.warning("概念映射加载失败，降级为空映射: %s", e)
        concept_map = {}
    print("[funnel] 加载概念热度...")
    try:
        concept_heat = fetch_concept_heat()
    except Exception as e:
        logger.warning("概念热度加载失败: %s", e)
        concept_heat = []
    if concept_heat:
        update_concept_heat_history(window.end_trade_date.isoformat(), concept_heat, top_n=cfg.theme_line_top_n)
    hot_concepts = detect_theme_lines(min_days=cfg.theme_line_min_days)
    print("[funnel] 加载市值数据...")
    try:
        market_cap_map = fetch_market_cap_map()
    except Exception as e:
        logger.warning("市值数据加载失败，降级为空映射: %s", e)
        market_cap_map = {}
    if not market_cap_map:
        print("[funnel] ⚠️ 市值数据为空（TUSHARE_TOKEN 可能缺失/失效），Layer1 将跳过市值过滤")
    # TickFlow 财务指标
    financial_map: dict[str, dict] = {}
    tickflow_api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if tickflow_api_key:
        try:
            from integrations.tickflow_client import TickFlowClient

            _tf = TickFlowClient(api_key=tickflow_api_key)
            print(f"[funnel] TickFlow 财务指标请求: symbols={len(all_symbols)}")
            raw_fin = _tf.get_financial_metrics(all_symbols, latest=True)
            for sym, records in raw_fin.items():
                if records:
                    financial_map[sym] = records[0]
            missing = max(len(all_symbols) - len(financial_map), 0)
            sample_missing = ",".join(sorted([s for s in all_symbols if s not in financial_map])[:8])
            print(
                f"[funnel] TickFlow 财务指标加载成功: {len(financial_map)}/{len(all_symbols)}, "
                f"missing={missing}, sample_missing={sample_missing or '-'}"
            )
        except Exception as e:
            logger.warning("TickFlow 财务指标加载失败，跳过财务过滤: %s", e)
    print("[funnel] 加载股票名称...")
    try:
        name_map = _stock_name_map()
    except Exception as e:
        logger.warning("股票名称加载失败，降级为代码展示: %s", e)
        name_map = {}

    bench_df, smallcap_df = _load_benchmark_indices(start_s, end_s)
    all_df_map, fetch_stats = fetch_all_ohlcv(
        symbols=all_symbols,
        window=window,
        enforce_target_trade_date=ENFORCE_TARGET_TRADE_DATE,
        batch_size=BATCH_SIZE,
        max_workers=MAX_WORKERS,
        batch_timeout=BATCH_TIMEOUT,
        batch_sleep=BATCH_SLEEP,
        executor_mode=EXECUTOR_MODE,
        direct_source=direct_source,
    )
    snapshot_dir = _dump_full_fetch_snapshot(
        df_map=all_df_map,
        all_symbols=all_symbols,
        window=window,
        fetch_stats=fetch_stats,
        bench_df=bench_df,
        smallcap_df=smallcap_df,
    )

    etf_symbols, etf_sector_map, etf_df_map, etf_l2_passed, etf_candidates = _run_etf_enhancement(
        cfg, window, bench_df, sector_map, all_df_map, direct_source=direct_source
    )

    breadth_context = _calc_market_breadth(all_df_map, BREADTH_MA_WINDOW)
    benchmark_context = _analyze_benchmark_and_tune_cfg(
        bench_df,
        smallcap_df,
        cfg,
        breadth=breadth_context,
    )
    print(
        "[funnel] 大盘总闸: "
        f"regime={benchmark_context['regime']}, "
        f"close={benchmark_context['close']}, ma50={benchmark_context['ma50']}, ma200={benchmark_context['ma200']}, "
        f"ma50_slope_5d={benchmark_context['ma50_slope_5d']}, main_today={benchmark_context.get('main_today_pct')}, recent3={benchmark_context['recent3_pct']}, "
        f"recent3_cum={benchmark_context['recent3_cum_pct']}, "
        f"smallcap_code={benchmark_context.get('smallcap_code')}, smallcap_today={benchmark_context.get('smallcap_today_pct')}, "
        f"breadth={benchmark_context.get('breadth')}, "
        f"panic_triggered={benchmark_context.get('panic_triggered')}, panic_reasons={benchmark_context.get('panic_reasons')}, "
        f"repair_triggered={benchmark_context.get('repair_triggered')}, repair_reasons={benchmark_context.get('repair_reasons')}, "
        f"tuned={benchmark_context['tuned']}"
    )

    print("[funnel] 开始执行全量漏斗筛选...")
    report_progress("漏斗筛选", "L1~L4 计算中", 0.85)

    l1_input = list(all_df_map.keys())
    l1_passed = layer1_filter(l1_input, name_map, market_cap_map, all_df_map, cfg, financial_map=financial_map)

    l2_passed, l2_channel_map, l2_pre_ignition = layer2_strength_detailed(
        l1_passed,
        all_df_map,
        bench_df,
        cfg,
        rps_universe=l1_input,
    )
    # 通道标签现在是多标签用 + 拼接，因此用 in 判断包含关系
    l2_momentum = sum(1 for v in l2_channel_map.values() if "主升通道" in v)
    l2_ambush = sum(1 for v in l2_channel_map.values() if "潜伏通道" in v)
    l2_accum = sum(1 for v in l2_channel_map.values() if "吸筹通道" in v)
    l2_dry_vol = sum(1 for v in l2_channel_map.values() if "地量蓄势" in v)
    l2_rs_div = sum(1 for v in l2_channel_map.values() if "暗中护盘" in v)
    l2_trend_cont = sum(1 for v in l2_channel_map.values() if "趋势延续" in v)
    l2_sos = sum(1 for v in l2_channel_map.values() if "点火破局" in v)

    # Layer 3 (Sector Resonance) — ETF L2 结果注入板块热度
    _etf_codes = set(etf_sector_map)
    l3_raw, top_sectors = layer3_sector_resonance(
        l2_passed + etf_l2_passed,
        sector_map,
        cfg,
        base_symbols=l1_passed + list(_etf_codes & set(etf_df_map)),
        df_map=all_df_map,
        concept_map=concept_map,
        hot_concepts=hot_concepts,
    )
    l3_passed = [s for s in l3_raw if s not in _etf_codes]
    sector_rotation = analyze_sector_rotation(
        all_df_map,
        sector_map,
        universe_symbols=list(all_df_map.keys()),
        focus_sectors=top_sectors,
    )
    benchmark_context["sector_rotation"] = sector_rotation
    print(f"[funnel] 板块轮动温度计: {sector_rotation.get('headline', '无')}")

    # Layer 4 (Wyckoff Triggers)
    # L4 需要 l2_df_map，这里直接用 all_df_map 即可，因为 key 都在里面
    triggers = layer4_triggers(l3_passed, all_df_map, cfg, channel_map=l2_channel_map, market_cap_map=market_cap_map)
    theme_radar_current = _safe_build_theme_radar(
        trade_date=window.end_trade_date.isoformat(),
        concept_heat=concept_heat,
        concept_map=concept_map,
        sector_map=sector_map,
        df_map=all_df_map,
        name_map=name_map,
    )
    theme_radar, theme_radar_source = _resolve_linked_theme_radar(
        theme_radar_current,
        window.end_trade_date.isoformat(),
    )
    theme_candidate_map = _theme_candidate_map(theme_radar)

    # TradingAgents 精选池注入：读 FUNNEL_EXTRA_SYMBOLS 环境变量
    # 如果标的已通过 L1，强制注入 L4 触发信号检测管道
    from tools.symbol_pool import _normalize_symbols as _normalize_extra_codes

    trading_agents_extra_codes: list[str] = []
    trading_agents_extra_in_l1: list[str] = []
    if FUNNEL_EXTRA_ENABLED:
        raw = FUNNEL_EXTRA_SYMBOLS_RAW
        parsed = [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]
        normalized = _normalize_extra_codes(parsed)
        l1_set = set(l1_passed)
        trading_agents_extra_codes = normalized
        trading_agents_extra_in_l1 = [c for c in normalized if c in l1_set]
        if trading_agents_extra_in_l1:
            print(
                f"[funnel] TradingAgents精选池: raw={len(parsed)}, "
                f"normalized={len(normalized)}, L1通过={len(trading_agents_extra_in_l1)}"
            )

    # L2 旁路观察池：L1通过 + L2被拒 + 在热门板块 + 有L4原始触发
    l2_rejected = [s for s in l1_passed if s not in set(l2_passed)]
    l2_bypass_in_sector = (
        [s for s in l2_rejected if str(sector_map.get(s, "")).strip() in set(top_sectors)] if top_sectors else []
    )
    # 把 TradingAgents 精选也注入 L4 检测（不需要在热门板块）
    if trading_agents_extra_in_l1:
        already_in = set(l2_bypass_in_sector)
        for c in trading_agents_extra_in_l1:
            if c not in already_in and c in l2_rejected:
                l2_bypass_in_sector.append(c)
    bypass_triggers: dict[str, list[tuple[str, float]]] = {}
    l2_bypass_pool: list[str] = []
    if l2_bypass_in_sector:
        bypass_triggers = layer4_triggers(
            l2_bypass_in_sector, all_df_map, cfg, channel_map=l2_channel_map, market_cap_map=market_cap_map
        )
        bypass_hit_set: set[str] = set()
        for hits in bypass_triggers.values():
            for code, _ in hits:
                bypass_hit_set.add(code)
        l2_bypass_pool = sorted(bypass_hit_set)
        if l2_bypass_pool:
            print(f"[funnel] L2旁路观察池: {len(l2_bypass_pool)} 只 (L2拒绝但有L4信号+板块共振)")

    strategic_seed_codes = _strategic_bypass_seed_codes(l1_passed, l2_passed, theme_candidate_map)
    strategic_bypass = _build_strategic_l2_bypass(
        strategic_seed_codes,
        all_df_map,
        cfg,
        l2_channel_map,
        market_cap_map,
    )
    strategic_l2_bypass_pool = list(strategic_bypass.get("pool") or [])
    strategic_l2_bypass_triggers = strategic_bypass.get("triggers") or {}
    strategic_l2_bypass_stage_map = strategic_bypass.get("stage_map") or {}
    strategic_l2_bypass_markup_symbols = strategic_bypass.get("markup_symbols") or []
    strategic_l2_bypass_reason_map = strategic_bypass.get("reason_map") or {}
    strategic_l2_bypass_rescue_map = strategic_bypass.get("rescue_map") or {}
    if strategic_l2_bypass_pool:
        print(
            "[funnel] 战略L2旁路: "
            f"seeds={len(strategic_seed_codes)}, pool={len(strategic_l2_bypass_pool)}, "
            f"L4={len(_trigger_hit_codes(strategic_l2_bypass_triggers))}, "
            f"stage={len(strategic_l2_bypass_reason_map)}, "
            f"rescue={len(strategic_l2_bypass_rescue_map)}"
        )

    # TradingAgents 精选池：L4 触发的注入到 l2_bypass_pool，未触发的留作哨兵追踪
    trading_agents_extra_l4_triggers: dict[str, list[tuple[str, float]]] = {}
    trading_agents_extra_watch: list[str] = []
    if trading_agents_extra_in_l1:
        ta_hit_set = set()
        for hits in bypass_triggers.values():
            for code, _score in hits:
                if str(code).strip() in set(trading_agents_extra_in_l1):
                    normalized_code = str(code).strip()
                    ta_hit_set.add(normalized_code)
                    for key, key_hits in bypass_triggers.items():
                        for hc, hs in key_hits:
                            if str(hc).strip() == normalized_code:
                                trading_agents_extra_l4_triggers.setdefault(key, []).append((normalized_code, hs))
        l2_bypass_pool_set = set(l2_bypass_pool)
        for code in sorted(ta_hit_set):
            if code not in l2_bypass_pool_set:
                l2_bypass_pool.append(code)
                l2_bypass_pool_set.add(code)
        # 未触发 L4 的精选标的 → 哨兵追踪池（留待 Step2.5 写 signal_pending）
        trading_agents_extra_watch = [c for c in trading_agents_extra_in_l1 if c not in ta_hit_set]
        if trading_agents_extra_watch:
            print(
                f"[funnel] TradingAgents哨兵追踪: {len(trading_agents_extra_watch)} 只 "
                f"(已过L1，尚无L4触发信号，T+{FUNNEL_EXTRA_TRACK_TTL_DAYS}日内持续追踪)"
            )

    # Markup 阶段、Accumulation ABC 细化、Exit 信号
    markup_symbols = sorted(
        set(detect_markup_stage(l3_passed, all_df_map, cfg)) | set(strategic_l2_bypass_markup_symbols)
    )
    accum_stage_map = detect_accum_stage(l2_passed, all_df_map, cfg)
    accum_stage_map.update(strategic_l2_bypass_stage_map)
    exit_signals = layer5_exit_signals(
        sorted(set(l2_passed + markup_symbols + strategic_l2_bypass_pool)),
        all_df_map,
        accum_stage_map,
        cfg,
    )

    total_hits = sum(len(v) for v in triggers.values())
    latest_close_map: dict[str, float] = {}
    for sym, df in all_df_map.items():
        try:
            close_series = pd.to_numeric(df.get("close"), errors="coerce")
            if close_series is None or close_series.empty:
                continue
            last_close = close_series.iloc[-1]
            if pd.notna(last_close):
                latest_close_map[str(sym).strip()] = float(last_close)
        except Exception:
            logger.debug("close price parse failed for %s", sym, exc_info=True)
            continue
    ranked_l3_symbols, l3_score_map = _rank_l3_candidates(
        l3_symbols=l3_passed,
        df_map=all_df_map,
        sector_map=sector_map,
        triggers=triggers,
        top_sectors=top_sectors,
        l2_channel_map=l2_channel_map,
        sector_rotation_map=(sector_rotation.get("state_map", {}) or {}),
    )
    metrics = {
        "total_symbols": len(all_symbols),
        "pool_mode": str(pool_stats.get("pool_mode", "") or ""),
        "pool_main": len(main_items),
        "pool_chinext": len(chinext_items),
        "pool_merged": len(merged_symbols),
        "pool_st_excluded": len(st_symbols),
        "pool_batches": total_batches,
        "end_trade_date": window.end_trade_date.isoformat(),
        "fetch_ok": int(fetch_stats.get("fetch_ok", len(all_df_map)) or 0),
        "fetch_fail": int(fetch_stats.get("fetch_fail", 0) or 0),
        "fetch_date_mismatch": int(fetch_stats.get("fetch_date_mismatch", 0) or 0),
        "fetch_spot_patched": int(fetch_stats.get("fetch_spot_patched", 0) or 0),
        "snapshot_dir": snapshot_dir,
        "layer1": len(l1_passed),
        "layer2": len(l2_passed),
        "layer2_momentum": l2_momentum,
        "layer2_ambush": l2_ambush,
        "layer2_accum": l2_accum,
        "layer2_dry_vol": l2_dry_vol,
        "layer2_rs_div": l2_rs_div,
        "layer2_trend_cont": l2_trend_cont,
        "layer2_sos": l2_sos,
        "layer2_channel_map": l2_channel_map,
        "layer3": len(l3_passed),
        "top_sectors": top_sectors,
        "concept_heat": concept_heat[:20],
        "concept_heat_full": concept_heat,
        "theme_lines": hot_concepts,
        "theme_radar": theme_radar,
        "theme_radar_current": theme_radar_current,
        "theme_radar_source": theme_radar_source,
        "candidate_concepts": {s: concept_map.get(s, []) for s in (ranked_l3_symbols or l3_passed)},
        "etf_enhancement": _etf_metrics(etf_symbols, etf_df_map, etf_l2_passed, etf_sector_map, etf_candidates),
        "etf_candidates": etf_candidates,
        "sector_rotation": sector_rotation,
        "layer3_symbols": ranked_l3_symbols or l3_passed,
        "layer3_score_map": l3_score_map,
        "total_hits": total_hits,
        "by_trigger": {k: len(v) for k, v in triggers.items()},
        "benchmark_context": benchmark_context,
        "latest_close_map": latest_close_map,
        "min_funnel_score": float(getattr(cfg, "min_funnel_score", 0.0) or 0.0),
        # L2 旁路观察池
        "l2_bypass_pool": l2_bypass_pool,
        "l2_bypass_triggers": bypass_triggers,
        "strategic_l2_bypass_seed_count": len(strategic_seed_codes),
        "strategic_l2_bypass_pool": strategic_l2_bypass_pool,
        "strategic_l2_bypass_triggers": strategic_l2_bypass_triggers,
        "strategic_l2_bypass_stage_map": strategic_l2_bypass_stage_map,
        "strategic_l2_bypass_rescue_map": strategic_l2_bypass_rescue_map,
        "strategic_l2_bypass_markup_symbols": strategic_l2_bypass_markup_symbols,
        "strategic_l2_bypass_reason_map": strategic_l2_bypass_reason_map,
        # TradingAgents 精选池追踪
        "trading_agents_extra_codes": trading_agents_extra_codes,
        "trading_agents_extra_in_l1": trading_agents_extra_in_l1,
        "trading_agents_extra_l4_triggers": trading_agents_extra_l4_triggers,
        "trading_agents_extra_watch": trading_agents_extra_watch,
        # 阶段识别和退出信号
        "markup_symbols": markup_symbols,
        "accum_stage_map": accum_stage_map,
        "exit_signals": exit_signals,
        "all_df_map": all_df_map,
        "financial_map": financial_map,
    }
    if include_debug_context:
        metrics["_debug"] = {
            "cfg": cfg,
            "end_trade_date": window.end_trade_date.isoformat(),
            "all_symbols": all_symbols,
            "name_map": name_map,
            "market_cap_map": market_cap_map,
            "sector_map": sector_map,
            "bench_df": bench_df,
            "all_df_map": all_df_map,
            "layer1_symbols": l1_passed,
            "layer2_symbols": l2_passed,
            "layer3_symbols_raw": l3_passed,
        }
    print(
        f"[funnel] L1={metrics['layer1']}, L2={metrics['layer2']}, "
        f"(主升={l2_momentum}, 潜伏={l2_ambush}, 吸筹={l2_accum}, 地量={l2_dry_vol}, 护盘={l2_rs_div}, 趋势={l2_trend_cont}, 点火={l2_sos}), "
        f"L3={metrics['layer3']}, 命中={total_hits}, "
        f"Top板块={top_sectors}, 主线={hot_concepts[:3] if hot_concepts else []}, "
        f"战略旁路={len(strategic_l2_bypass_pool)}, 各触发={metrics['by_trigger']}"
    )
    print(f"[funnel] 主题雷达({theme_radar_source}): {summarize_theme_radar(theme_radar)}")
    report_progress("筛选完成", f"命中={total_hits}只", 1.0)

    return triggers, metrics


def run(
    webhook_url: str,
    *,
    notify: bool = True,
    return_details: bool = False,
) -> tuple[bool, list[dict], dict] | tuple[bool, list[dict], dict, dict]:
    """
    执行 Wyckoff Funnel，漏斗完成后立即发送飞书通知。
    返回 (成功与否, 用于研报的股票信息列表, 大盘上下文)。
    每项为 {"code": str, "name": str, "tag": str}。
    """
    triggers, metrics = run_funnel_job()
    all_df_map = metrics.get("all_df_map", {})
    benchmark_context = metrics.get("benchmark_context", {}) or {}
    try:
        name_map = _stock_name_map()
    except Exception as e:
        logger.warning("股票名称加载失败，降级为代码展示: %s", e)
        name_map = {}
    try:
        sector_map = fetch_sector_map()
    except Exception as e:
        logger.warning("行业映射加载失败，降级为空映射: %s", e)
        sector_map = {}
    latest_close_map = metrics.get("latest_close_map", {}) or {}
    if latest_close_map:
        benchmark_context["latest_close_map"] = latest_close_map

    theme_radar = metrics.get("theme_radar") or {}
    theme_candidate_map = _theme_candidate_map(theme_radar)
    theme_badge_map = _theme_badge_map(theme_candidate_map)
    theme_bonus_map = _theme_bonus_map(theme_candidate_map)
    l2_bypass_pool = metrics.get("l2_bypass_pool", []) or []
    bypass_triggers = metrics.get("l2_bypass_triggers", {}) or {}
    strategic_l2_bypass_pool = metrics.get("strategic_l2_bypass_pool", []) or []
    strategic_l2_bypass_triggers = metrics.get("strategic_l2_bypass_triggers", {}) or {}
    strategic_l2_bypass_reason_map = metrics.get("strategic_l2_bypass_reason_map", {}) or {}
    strategic_l2_bypass_stage_map = metrics.get("strategic_l2_bypass_stage_map", {}) or {}
    trading_agents_extra_codes = metrics.get("trading_agents_extra_codes", []) or []
    review_triggers = _merge_trigger_maps(triggers, bypass_triggers, strategic_l2_bypass_triggers)
    formal_hit_set = {str(code).strip() for hits in triggers.values() for code, _ in hits if str(code).strip()}
    l2_bypass_set = set(l2_bypass_pool)
    strategic_l2_bypass_set = {str(c).strip() for c in strategic_l2_bypass_pool if str(c).strip()}
    code_to_reasons: dict[str, list[str]] = {}
    code_to_trigger_keys: dict[str, list[str]] = {}
    code_to_total_score: dict[str, float] = {}
    for key, label in TRIGGER_LABELS.items():
        for code, score in review_triggers.get(key, []):
            if code not in code_to_reasons:
                code_to_reasons[code] = []
                code_to_trigger_keys[code] = []
                code_to_total_score[code] = 0.0
            code_to_reasons[code].append(label)
            code_to_trigger_keys[code].append(key)
            code_to_total_score[code] += score

    for code in strategic_l2_bypass_set:
        code_to_reasons.setdefault(code, [])
        code_to_trigger_keys.setdefault(code, [])
        code_to_total_score.setdefault(code, 0.0)
    # TradingAgents 精选池：注入基础分数 + 标签
    extra_bonus = float(os.getenv("FUNNEL_EXTRA_BONUS_SCORE", "15.0"))
    for code in trading_agents_extra_codes:
        code_to_reasons.setdefault(code, [])
        code_to_trigger_keys.setdefault(code, [])
        if code not in code_to_total_score or code_to_total_score[code] == 0.0:
            code_to_total_score[code] = extra_bonus
        if "TradingAgents" not in code_to_reasons[code]:
            code_to_reasons[code].append("TradingAgents")
    _append_extra_reasons(code_to_reasons, strategic_l2_bypass_reason_map)
    _append_theme_reasons(code_to_reasons, theme_badge_map)
    _apply_theme_bonus_to_scores(code_to_total_score, theme_bonus_map)
    # 兼容旧变量名（下游可能引用）
    code_to_best_score = code_to_total_score
    sorted_codes = sorted(
        code_to_reasons.keys(),
        key=lambda c: -code_to_total_score.get(c, 0),
    )
    formal_sorted_codes = [code for code in sorted_codes if code in formal_hit_set]
    unique_hit_count = len(formal_hit_set)
    review_unique_count = len(sorted_codes)
    l2_bypass_ranked = _rank_l2_bypass_pool(l2_bypass_pool, code_to_total_score)
    strategic_l2_bypass_ranked = _rank_l2_bypass_pool(strategic_l2_bypass_pool, code_to_total_score)
    use_full_formal_l4_selection = FUNNEL_AI_SELECTION_MODE in {
        "all_formal_l4",
        "all_l4",
        "full_formal_l4",
        "full_l4",
    }
    use_legacy_selection = FUNNEL_AI_SELECTION_MODE in {
        "legacy_full_hits",
        "legacy_hits",
        "all_hits",
        "classic",
    }
    use_legacy_card = FUNNEL_CARD_STYLE in {
        "legacy",
        "legacy_compact",
        "classic",
        "v1",
    }
    l3_ranked_symbols = [str(c).strip() for c in (metrics.get("layer3_symbols", []) or []) if str(c).strip()]
    l2_channel_map = metrics.get("layer2_channel_map", {}) or {}
    # 提前取出，供后面的闭包函数引用
    markup_symbols = metrics.get("markup_symbols", []) or []
    accum_stage_map = metrics.get("accum_stage_map", {}) or {}
    exit_signals = metrics.get("exit_signals", {}) or {}
    sector_rotation = metrics.get("sector_rotation", {}) or {}
    sector_rotation_map = sector_rotation.get("state_map", {}) or {}
    etf_metrics = metrics.get("etf_enhancement", {}) or {}
    etf_candidates = metrics.get("etf_candidates", []) or []
    theme_l4_count = sum(1 for c in formal_hit_set if c in theme_candidate_map)
    theme_radar_source = str(metrics.get("theme_radar_source") or "current")
    strategic_accum_codes = {
        str(code).strip()
        for code, stage in strategic_l2_bypass_stage_map.items()
        if str(stage or "").strip() in {"Accum_B", "Accum_C"}
    }
    # 策略：大盘水温驱动的双轨制（Top-Down 择时顺势策略）
    regime = benchmark_context.get("regime", "NEUTRAL")
    if use_full_formal_l4_selection or use_legacy_selection:
        selected_for_ai = list(formal_sorted_codes)
        trend_selected, accum_selected = _split_selected_tracks(selected_for_ai, code_to_trigger_keys)
        score_map = {c: float(code_to_best_score.get(c, 0.0)) for c in formal_sorted_codes}
        ai_policy = {
            "total_cap": len(formal_sorted_codes),
            "trend_quota": len(trend_selected),
            "accum_quota": len(accum_selected),
            "requested_trend_quota": len(trend_selected),
            "requested_accum_quota": len(accum_selected),
            "quota_family": "FULL_FORMAL_L4",
            "max_trend_l3_fill": 0,
            "max_accum_l3_fill": 0,
        }
        print(
            f"[funnel] AI候选分配完成(full_formal_l4): "
            f"Trend={len(trend_selected)}, Accum={len(accum_selected)}, total={len(selected_for_ai)}"
        )
        if dynamic_policy_mode() == "shadow":
            _attach_shadow_policy(ai_policy, _load_dynamic_policy_context(str(regime), benchmark_context))
    else:
        trend_selected, accum_selected, score_map, ai_policy = _allocate_candidates_for_ai(
            metrics,
            triggers,
            l3_ranked_symbols,
            str(regime),
            sector_map,
            benchmark_context,
        )
        selected_for_ai = trend_selected + accum_selected

    if not (use_full_formal_l4_selection or use_legacy_selection):
        _apply_theme_bonus_to_scores(score_map, theme_bonus_map)

    bypass_added = _promote_l2_bypass_for_ai(
        selected_for_ai,
        trend_selected,
        accum_selected,
        l2_bypass_pool,
        code_to_total_score,
        code_to_trigger_keys,
        score_map,
    )
    if bypass_added:
        print(
            f"[funnel] L2明珠旁路送审: added={bypass_added}, "
            f"budget={FUNNEL_L2_BYPASS_AI_CAP or 'unlimited'}, pool={len(l2_bypass_pool)}"
        )

    strategic_bypass_added = _promote_l2_bypass_for_ai(
        selected_for_ai,
        trend_selected,
        accum_selected,
        strategic_l2_bypass_pool,
        code_to_total_score,
        code_to_trigger_keys,
        score_map,
        enabled=FUNNEL_STRATEGIC_L2_BYPASS_ENABLED,
        cap=FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP,
        accum_codes=strategic_accum_codes,
    )
    if strategic_bypass_added:
        print(
            f"[funnel] 战略L2旁路送审: added={strategic_bypass_added}, "
            f"budget={FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP or 'unlimited'}, "
            f"pool={len(strategic_l2_bypass_pool)}"
        )

    theme_promoted_count = _promote_theme_l4_for_ai(
        selected_for_ai,
        trend_selected,
        accum_selected,
        formal_hit_set,
        theme_bonus_map,
        code_to_total_score,
        code_to_trigger_keys,
        score_map,
    )
    if theme_promoted_count:
        print(
            f"[funnel] 战略主线联动送审: added={theme_promoted_count}, "
            f"cap={FUNNEL_THEME_RADAR_PROMOTE_CAP or 'unlimited'}, l4_hit={theme_l4_count}"
        )

    min_funnel_score = float(metrics.get("min_funnel_score", 0.0) or 0.0)
    if score_map and min_funnel_score > 0:
        before = len(selected_for_ai)
        selected_for_ai = [c for c in selected_for_ai if score_map.get(c, 0.0) >= min_funnel_score]
        selected_set = set(selected_for_ai)
        trend_selected = [c for c in trend_selected if c in selected_set]
        accum_selected = [c for c in accum_selected if c in selected_set]
        dropped = before - len(selected_for_ai)
        if dropped:
            print(f"[funnel] min_funnel_score={min_funnel_score} 过滤掉 {dropped} 只低质量候选")

    selected_for_ai = _rerank_selected_codes(selected_for_ai, score_map)
    trend_set = set(trend_selected)
    accum_set = set(accum_selected)
    trend_selected = [c for c in selected_for_ai if c in trend_set]
    accum_selected = [c for c in selected_for_ai if c in accum_set]

    shadow_meta = _maybe_persist_policy_shadow_run(
        ai_policy=ai_policy,
        metrics=metrics,
        triggers=triggers,
        selected_for_ai=selected_for_ai,
        l3_ranked_symbols=l3_ranked_symbols,
        regime=str(regime),
        sector_map=sector_map,
    )
    ai_policy.update(shadow_meta)

    if use_legacy_card and use_legacy_selection:
        bench_line = "未知"
        pv_line = "暂无大盘量价推演"
        if benchmark_context:
            _close = benchmark_context.get("close") or 0
            _ma50 = benchmark_context.get("ma50") or 0
            _ma200 = benchmark_context.get("ma200") or 0
            _cum3 = benchmark_context.get("recent3_cum_pct") or 0
            bench_line = (
                f"{benchmark_context.get('regime')} | "
                f"收盘 {float(_close):.2f} | MA50 {float(_ma50):.2f} | MA200 {float(_ma200):.2f} | "
                f"近3日 {float(_cum3):+.2f}%"
            )
            pv_line = str(
                benchmark_context.get("market_pv_outlook") or benchmark_context.get("market_pv_summary") or pv_line
            )

        lines = [
            (
                f"**股票池**: 主板{metrics['pool_main']} + 创业板{metrics['pool_chinext']} "
                f"-> 去重{metrics['pool_merged']} -> 去ST{metrics['pool_st_excluded']} "
                f"= {metrics['total_symbols']} (共{metrics['pool_batches']}批)"
            ),
            f"**漏斗概览**: {metrics['total_symbols']}只 → L1:{metrics['layer1']} → L2:{metrics['layer2']} → L3:{metrics['layer3']} → 命中:{metrics['total_hits']}",
            f"**大盘水温**: {bench_line}",
            f"**大盘量价推演**: {pv_line}",
            f"**中长线主线**: {summarize_theme_radar(metrics.get('theme_radar') or {})} ({theme_radar_source})",
            (
                f"**战略主线联动**: 观察池{len(theme_candidate_map)}只 / "
                f"正式L4命中{theme_l4_count}只 / 战略L2旁路{len(strategic_l2_bypass_pool)}只 / "
                f"加权送审{theme_promoted_count}只"
            ),
            f"**候选分层**: 正式L4命中{unique_hit_count}只 / L2明珠池{len(l2_bypass_pool)}只 "
            f"-> AI输入{len(selected_for_ai)}只 "
            f"(正式L4 {sum(1 for c in selected_for_ai if c in formal_hit_set)} / "
            f"L2明珠 {sum(1 for c in selected_for_ai if c in l2_bypass_set)} / "
            f"战略旁路 {sum(1 for c in selected_for_ai if c in strategic_l2_bypass_set)}; "
            f"旁路预算 {FUNNEL_L2_BYPASS_AI_CAP or 'unlimited'})",
            f"**Top 行业**: {', '.join(metrics['top_sectors']) if metrics['top_sectors'] else '无'}",
            "",
        ]
        _append_etf_section(lines, etf_metrics, etf_candidates)
        if etf_metrics or etf_candidates:
            lines.append("")

        # ── 命中列表：按信号分组 ──
        def _score_star(s: float) -> str:
            if s >= 10:
                return "★★"
            if s >= 5:
                return "★ "
            return "  "

        # 1) 多信号共振组（置顶）
        multi_signal = [
            c for c in selected_for_ai if c not in strategic_l2_bypass_set and len(code_to_trigger_keys.get(c, [])) > 1
        ]
        if multi_signal:
            lines.append(f"**【🔥 多信号共振】{len(multi_signal)} 只**")
            for code in sorted(multi_signal, key=lambda c: -code_to_total_score.get(c, 0)):
                name = name_map.get(code, code)
                short = "+".join(TRIGGER_SHORT_LABELS.get(k, k) for k in code_to_trigger_keys.get(code, []))
                score = code_to_total_score.get(code, 0)
                theme_badge = f"  {theme_badge_map[code]}" if code in theme_badge_map else ""
                lines.append(f"{_score_star(score)} {code} {name}  {score:.2f}  {short}{theme_badge}")
            lines.append("")

        # 2) 各信号分组
        single_signal_codes = [
            c for c in selected_for_ai if c not in set(multi_signal) and c not in strategic_l2_bypass_set
        ]
        # 为每个 code 确定主信号（取第一个 trigger key）
        code_primary_key: dict[str, str] = {}
        for code in single_signal_codes:
            keys = code_to_trigger_keys.get(code, [])
            code_primary_key[code] = keys[0] if keys else "sos"

        for group_key in TRIGGER_GROUP_ORDER:
            group_codes = [c for c in single_signal_codes if code_primary_key.get(c) == group_key]
            if not group_codes:
                continue
            group_title = TRIGGER_GROUP_TITLES.get(group_key, group_key)
            lines.append(f"**【{group_title}】{len(group_codes)} 只**")
            for code in sorted(group_codes, key=lambda c: -code_to_total_score.get(c, 0)):
                name = name_map.get(code, code)
                score = code_to_total_score.get(code, 0)
                theme_badge = f"  {theme_badge_map[code]}" if code in theme_badge_map else ""
                lines.append(f"{_score_star(score)} {code} {name}  {score:.2f}{theme_badge}")
            lines.append("")

        if not selected_for_ai:
            lines.append("无")

        if l2_bypass_pool:
            lines.append("")
            lines.append(f"**【👁 L2旁路观察】{len(l2_bypass_pool)} 只**")
            lines.append(
                f"未过L2强度，按形态分数排序；送AI复核 {sum(1 for c in selected_for_ai if c in l2_bypass_set)} 只"
            )
            display_pool = (
                l2_bypass_ranked if FUNNEL_BYPASS_DISPLAY_LIMIT <= 0 else l2_bypass_ranked[:FUNNEL_BYPASS_DISPLAY_LIMIT]
            )
            for code in display_pool:
                name = name_map.get(code, code)
                bp_reasons = []
                for key, _label in TRIGGER_LABELS.items():
                    for c, _ in bypass_triggers.get(key, []):
                        if c == code:
                            bp_reasons.append(TRIGGER_SHORT_LABELS.get(key, key))
                industry = str(sector_map.get(code, "") or "")
                theme_badge = f"  {theme_badge_map[code]}" if code in theme_badge_map else ""
                lines.append(f"  {code} {name}  {'+'.join(bp_reasons)}  [{industry}]{theme_badge}")
            omitted = len(l2_bypass_pool) - len(display_pool)
            if omitted > 0:
                lines.append(f"  ... 另 {omitted} 只略")

        if strategic_l2_bypass_pool:
            lines.append("")
            lines.append(f"**【🧭 战略L2旁路】{len(strategic_l2_bypass_pool)} 只**")
            lines.append(
                f"L1通过但L2未过，需同时满足战略观察池与L4/阶段复核；"
                f"送AI复核 {sum(1 for c in selected_for_ai if c in strategic_l2_bypass_set)} 只"
            )
            display_pool = (
                strategic_l2_bypass_ranked
                if FUNNEL_BYPASS_DISPLAY_LIMIT <= 0
                else strategic_l2_bypass_ranked[:FUNNEL_BYPASS_DISPLAY_LIMIT]
            )
            for code in display_pool:
                name = name_map.get(code, code)
                short = "+".join(TRIGGER_SHORT_LABELS.get(k, k) for k in code_to_trigger_keys.get(code, []))
                stage = str(strategic_l2_bypass_stage_map.get(code, "") or "").strip()
                reason = " / ".join(x for x in [short, stage] if x) or "战略复核"
                theme_badge = f"  {theme_badge_map[code]}" if code in theme_badge_map else ""
                lines.append(f"  {code} {name}  {reason}{theme_badge}")
            omitted = len(strategic_l2_bypass_pool) - len(display_pool)
            if omitted > 0:
                lines.append(f"  ... 另 {omitted} 只略")

        content = "\n".join(lines)
        title = f"🔬 Wyckoff Funnel {date.today().strftime('%Y-%m-%d')}"
        ok = True if not notify else send_feishu_notification(webhook_url, title, content)

        sos_hit_set = {str(c).strip() for c, _ in review_triggers.get("sos", [])}
        evr_hit_set = {str(c).strip() for c, _ in review_triggers.get("evr", [])}
        spring_hit_set = {str(c).strip() for c, _ in review_triggers.get("spring", [])}
        lps_hit_set = {str(c).strip() for c, _ in review_triggers.get("lps", [])}

        def _infer_track(code: str) -> str:
            if code in sos_hit_set or code in evr_hit_set:
                return "Trend"
            if code in spring_hit_set or code in lps_hit_set:
                return "Accum"
            return "Trend"

        def _legacy_stage(code: str) -> str:
            if code in markup_symbols:
                return "Markup"
            return str(accum_stage_map.get(code, "") or "").strip()

        def _legacy_score(code: str) -> float:
            return float(
                code_to_total_score.get(code, 0.0) or (metrics.get("layer3_score_map", {}) or {}).get(code, 0.0)
            )

        symbols_for_report = [
            {
                "code": c,
                "name": name_map.get(c, c),
                "tag": _candidate_reason_text(c, code_to_reasons, theme_badge_map),
                "track": _infer_track(c),
                "stage": _legacy_stage(c),
                "score": _legacy_score(c),
                **_signal_report_fields(c, code_to_trigger_keys, _infer_track(c), str(regime), _legacy_score(c)),
                "priority_score": float(code_to_best_score.get(c, 0.0)),
                "priority_rank": idx + 1,
                "selection_source": (
                    "strategic_l2_bypass"
                    if c in strategic_l2_bypass_set
                    else "l2_bypass"
                    if c in l2_bypass_set
                    else "l4_hit"
                ),
                "selection_is_fill": False,
                "initial_price": float(latest_close_map.get(c, 0.0) or 0.0),
                "industry": str(sector_map.get(c, "") or "未知行业"),
                "sector_state_code": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("state", "")
                ).strip(),
                "sector_state": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get(
                        "label",
                        "",
                    )
                ).strip(),
                "sector_note": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("note", "")
                ).strip(),
                "sector_guidance": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("guidance", "")
                ).strip(),
                "exit_signal": str((exit_signals.get(c, {}) or {}).get("signal", "")).strip(),
                "exit_price": (exit_signals.get(c, {}) or {}).get("price"),
                "exit_reason": str((exit_signals.get(c, {}) or {}).get("reason", "")).strip(),
                **_theme_report_fields(c, theme_candidate_map, theme_bonus_map),
            }
            for idx, c in enumerate(selected_for_ai)
        ]
        if return_details:
            details = {
                "metrics": metrics,
                "triggers": review_triggers,
                "review_triggers": review_triggers,
                "formal_triggers": triggers,
                "l2_bypass_triggers": bypass_triggers,
                "l2_bypass_selected": [c for c in selected_for_ai if c in l2_bypass_set],
                "l2_bypass_budget": FUNNEL_L2_BYPASS_AI_CAP,
                "strategic_l2_bypass_triggers": strategic_l2_bypass_triggers,
                "strategic_l2_bypass_selected": [c for c in selected_for_ai if c in strategic_l2_bypass_set],
                "strategic_l2_bypass_budget": FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP,
                "content": content,
                "title": title,
                "symbols_for_report": symbols_for_report,
                "selected_for_ai": selected_for_ai,
                "trend_selected": [],
                "accum_selected": [],
                "priority_score_map": score_map,
                "name_map": name_map,
                "sector_map": sector_map,
                "all_df_map": all_df_map,
            }
            return (ok, symbols_for_report, benchmark_context, details)
        return (ok, symbols_for_report, benchmark_context)

    formal_event_count = sum(len(v) for v in triggers.values())
    bypass_selected_count = sum(1 for c in selected_for_ai if c in l2_bypass_set)
    strategic_bypass_selected_count = sum(1 for c in selected_for_ai if c in strategic_l2_bypass_set)

    def _stage_name(code: str) -> str:
        if code in markup_symbols:
            return "Markup"
        return str(accum_stage_map.get(code, "") or "").strip()

    hit_selected_count = sum(1 for c in selected_for_ai if c in formal_hit_set)
    l3_only_count = max(
        len(selected_for_ai) - hit_selected_count - bypass_selected_count - strategic_bypass_selected_count, 0
    )
    sector_rotation = metrics.get("sector_rotation", {}) or {}
    sector_rotation_map = sector_rotation.get("state_map", {}) or {}

    def _selected_track(code: str) -> str:
        return "Trend" if code in trend_selected else "Accum" if code in accum_selected else ""

    total_cap = int(ai_policy["total_cap"])
    trend_quota = int(ai_policy["trend_quota"])
    accum_quota = int(ai_policy["accum_quota"])
    requested_trend_quota = int(ai_policy["requested_trend_quota"])
    requested_accum_quota = int(ai_policy["requested_accum_quota"])
    quota_family = str(ai_policy["quota_family"])
    max_trend_l3_fill = int(ai_policy["max_trend_l3_fill"])
    max_accum_l3_fill = int(ai_policy["max_accum_l3_fill"])

    print(
        f"[funnel] 候选分层: 正式L4事件={formal_event_count}, 正式命中股票={unique_hit_count}, "
        f"L2明珠池={len(l2_bypass_pool)}, review候选={review_unique_count}, "
        f"配额配置=[{regime}->{quota_family}: requested Trend={requested_trend_quota}, "
        f"requested Accum={requested_accum_quota}, effective Trend={trend_quota}, "
        f"effective Accum={accum_quota}, 总上限={total_cap}, "
        f"l3_fill_limit Trend={max_trend_l3_fill}, Accum={max_accum_l3_fill}], "
        f"最终选入: Trend={len(trend_selected)}, Accum={len(accum_selected)}, "
        f"战略旁路={strategic_bypass_selected_count}, 总计={len(selected_for_ai)}"
    )

    bench_line = "未知"
    pv_line = "暂无大盘量价推演"
    if benchmark_context:
        _close = benchmark_context.get("close") or 0
        _ma50 = benchmark_context.get("ma50") or 0
        _ma200 = benchmark_context.get("ma200") or 0
        _cum3 = benchmark_context.get("recent3_cum_pct") or 0
        bench_line = (
            f"{benchmark_context.get('regime')} | 收盘 {float(_close):.2f} | "
            f"MA50 {float(_ma50):.2f} | MA200 {float(_ma200):.2f} | 近3日 {float(_cum3):+.2f}%"
        )
        pv_line = str(
            benchmark_context.get("market_pv_outlook") or benchmark_context.get("market_pv_summary") or pv_line
        )

    lines = [
        (
            f"**股票池**: 主板{metrics['pool_main']} + 创业板{metrics['pool_chinext']} "
            f"-> 去重{metrics['pool_merged']} -> 去ST{metrics['pool_st_excluded']} "
            f"= {metrics['total_symbols']} (共{metrics['pool_batches']}批)"
        ),
        f"**漏斗概览**: {metrics['total_symbols']}只 → L1:{metrics['layer1']} → L2:{metrics['layer2']} → L3:{metrics['layer3']} → 正式L4:{unique_hit_count}",
        f"**大盘水温**: {bench_line}",
        f"**大盘量价推演**: {pv_line}",
        f"**中长线主线**: {summarize_theme_radar(metrics.get('theme_radar') or {})} ({theme_radar_source})",
        (
            f"**战略主线联动**: 观察池{len(theme_candidate_map)}只 / "
            f"正式L4命中{theme_l4_count}只 / 战略L2旁路{len(strategic_l2_bypass_pool)}只 / "
            f"加权送审{theme_promoted_count}只"
        ),
        (
            f"**候选分层**: 正式L4命中{unique_hit_count}只 / L2明珠池{len(l2_bypass_pool)}只 "
            f"-> AI输入{len(selected_for_ai)}只 "
            f"(配额 {quota_family}: Trend {len(trend_selected)}/{trend_quota}, "
            f"Accum {len(accum_selected)}/{accum_quota}; "
            f"正式L4 {hit_selected_count} / L3补充{l3_only_count} / "
            f"L2明珠 {bypass_selected_count} / 战略旁路 {strategic_bypass_selected_count}; "
            f"旁路预算 {FUNNEL_L2_BYPASS_AI_CAP or 'unlimited'})"
        ),
        f"**Top 行业**: {', '.join(metrics['top_sectors']) if metrics['top_sectors'] else '无'}",
        "",
    ]
    if ai_policy.get("shadow_table"):
        lines.insert(
            -1,
            f"**动态策略 Shadow**: `{ai_policy['shadow_table']}` 写入{ai_policy.get('shadow_written', 0)}行；"
            f"shadow新增{ai_policy.get('shadow_added_count', 0)}只，移除{ai_policy.get('shadow_removed_count', 0)}只",
        )
    _append_etf_section(lines, etf_metrics, etf_candidates)
    if etf_metrics or etf_candidates:
        lines.append("")

    def _display_score(code: str) -> float:
        trigger_score = float(code_to_total_score.get(code, 0.0) or 0.0)
        return trigger_score if trigger_score > 0 else float(score_map.get(code, 0.0) or 0.0)

    if formal_sorted_codes:
        lines.append("**正式L4展开**: 以下列出全部正式L4；标记 →AI 的进入 Step3 研报")
        _append_formal_l4_sections(
            lines,
            formal_sorted_codes,
            selected_for_ai,
            name_map,
            code_to_trigger_keys,
            _display_score,
            theme_badge_map,
        )

    fill_codes = [
        c
        for c in selected_for_ai
        if c not in formal_hit_set and c not in l2_bypass_set and c not in strategic_l2_bypass_set
    ]
    if fill_codes:
        lines.append(f"**【🧭 L3/阶段补位】{len(fill_codes)} 只**")
        for code in sorted(fill_codes, key=lambda c: -_display_score(c)):
            name = name_map.get(code, code)
            stage = _stage_name(code)
            channel = str(l2_channel_map.get(code, "")).strip()
            suffix = " / ".join(x for x in [stage, channel] if x)
            score = _display_score(code)
            theme_badge = f"  {theme_badge_map[code]}" if code in theme_badge_map else ""
            lines.append(
                f"{_score_star(score)} {code} {name}  {score:.2f}" + (f"  {suffix}" if suffix else "") + theme_badge
            )
        lines.append("")

    if not selected_for_ai:
        lines.append("无")

    if l2_bypass_pool:
        lines.append("")
        lines.append(f"**【👁 L2旁路观察】{len(l2_bypass_pool)} 只**")
        lines.append(f"未过L2强度，按形态分数排序；送AI复核 {bypass_selected_count} 只")
        display_pool = (
            l2_bypass_ranked if FUNNEL_BYPASS_DISPLAY_LIMIT <= 0 else l2_bypass_ranked[:FUNNEL_BYPASS_DISPLAY_LIMIT]
        )
        for code in display_pool:
            bp_name = name_map.get(code, code)
            bp_reasons = []
            for key in TRIGGER_LABELS:
                for c, _ in bypass_triggers.get(key, []):
                    if c == code:
                        bp_reasons.append(TRIGGER_SHORT_LABELS.get(key, key))
            bp_industry = str(sector_map.get(code, "") or "")
            theme_badge = f"  {theme_badge_map[code]}" if code in theme_badge_map else ""
            lines.append(f"  {code} {bp_name}  {'+'.join(bp_reasons)}  [{bp_industry}]{theme_badge}")
        omitted = len(l2_bypass_pool) - len(display_pool)
        if omitted > 0:
            lines.append(f"  ... 另 {omitted} 只略")

    if strategic_l2_bypass_pool:
        lines.append("")
        lines.append(f"**【🧭 战略L2旁路】{len(strategic_l2_bypass_pool)} 只**")
        lines.append(
            f"L1通过但L2未过，需同时满足战略观察池与L4/阶段复核；送AI复核 {strategic_bypass_selected_count} 只"
        )
        display_pool = (
            strategic_l2_bypass_ranked
            if FUNNEL_BYPASS_DISPLAY_LIMIT <= 0
            else strategic_l2_bypass_ranked[:FUNNEL_BYPASS_DISPLAY_LIMIT]
        )
        for code in display_pool:
            bp_name = name_map.get(code, code)
            bp_reasons = []
            for key in TRIGGER_LABELS:
                for c, _ in strategic_l2_bypass_triggers.get(key, []):
                    if c == code:
                        bp_reasons.append(TRIGGER_SHORT_LABELS.get(key, key))
            stage = str(strategic_l2_bypass_stage_map.get(code, "") or "").strip()
            reason = " / ".join(x for x in ["+".join(bp_reasons), stage] if x) or "战略复核"
            theme_badge = f"  {theme_badge_map[code]}" if code in theme_badge_map else ""
            lines.append(f"  {code} {bp_name}  {reason}{theme_badge}")
        omitted = len(strategic_l2_bypass_pool) - len(display_pool)
        if omitted > 0:
            lines.append(f"  ... 另 {omitted} 只略")

    content = "\n".join(lines)
    title = f"🔬 Wyckoff Funnel {date.today().strftime('%Y-%m-%d')}"
    ok = True if not notify else send_feishu_notification(webhook_url, title, content)

    def _selection_source(code: str) -> str:
        if code in strategic_l2_bypass_set:
            return "strategic_l2_bypass"
        if code in l2_bypass_set:
            return "l2_bypass"
        if code in formal_hit_set:
            return "l4_hit"
        if code in markup_symbols:
            return "markup"
        if _stage_name(code) == "Accum_C":
            return "accum_c"
        return "l3_fill"

    symbols_for_report = [
        {
            "code": c,
            "name": name_map.get(c, c),
            "tag": (
                f"{'战略L2旁路' if c in strategic_l2_bypass_set else 'L2旁路观察' if c in l2_bypass_set else str(l2_channel_map.get(c, '')).strip()} | "
                f"{_candidate_reason_text(c, code_to_reasons, theme_badge_map)}"
            ).strip(" |"),
            "track": _selected_track(c),
            "stage": _stage_name(c),
            "score": float(_display_score(c)),
            **_signal_report_fields(c, code_to_trigger_keys, _selected_track(c), str(regime), _display_score(c)),
            "priority_score": float(score_map.get(c, 0.0)),
            "priority_rank": idx + 1,
            "selection_source": _selection_source(c),
            "selection_is_fill": _selection_source(c) == "l3_fill",
            "initial_price": float(latest_close_map.get(c, 0.0) or 0.0),
            "industry": str(sector_map.get(c, "") or "未知行业"),
            "sector_state_code": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("state", "")
            ).strip(),
            "sector_state": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get(
                    "label",
                    "",
                )
            ).strip(),
            "sector_note": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("note", "")
            ).strip(),
            "sector_guidance": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("guidance", "")
            ).strip(),
            "exit_signal": str((exit_signals.get(c, {}) or {}).get("signal", "")).strip(),
            "exit_price": (exit_signals.get(c, {}) or {}).get("price"),
            "exit_reason": str((exit_signals.get(c, {}) or {}).get("reason", "")).strip(),
            **_theme_report_fields(c, theme_candidate_map, theme_bonus_map),
        }
        for idx, c in enumerate(selected_for_ai)
    ]
    if return_details:
        details = {
            "metrics": metrics,
            "triggers": review_triggers,
            "review_triggers": review_triggers,
            "formal_triggers": triggers,
            "l2_bypass_triggers": bypass_triggers,
            "l2_bypass_selected": [c for c in selected_for_ai if c in l2_bypass_set],
            "l2_bypass_budget": FUNNEL_L2_BYPASS_AI_CAP,
            "strategic_l2_bypass_triggers": strategic_l2_bypass_triggers,
            "strategic_l2_bypass_selected": [c for c in selected_for_ai if c in strategic_l2_bypass_set],
            "strategic_l2_bypass_budget": FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP,
            "content": content,
            "title": title,
            "symbols_for_report": symbols_for_report,
            "selected_for_ai": selected_for_ai,
            "trend_selected": trend_selected,
            "accum_selected": accum_selected,
            "priority_score_map": score_map,
            "name_map": name_map,
            "sector_map": sector_map,
            "all_df_map": all_df_map,
        }
        return (ok, symbols_for_report, benchmark_context, details)
    return (ok, symbols_for_report, benchmark_context)
