"""Real-time A-share limit-up ladder, deep-water reversal, and observation scanner."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = Path(__file__).resolve().parents[4]
for _p in (str(SCRIPT_DIR), str(ROOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from realtime_concepts import fetch_realtime_concept_bundle  # noqa: E402
from sniper_feishu import (  # noqa: E402
    compute_market_sentiment,
    fetch_daily_overhead_map,
    push_sniper_feishu_alert,
    select_sniper_targets,
)

from integrations.tickflow_client import TickFlowClient, normalize_cn_symbol  # noqa: E402

SH_TZ = ZoneInfo("Asia/Shanghai")
VWAP_TOL = 1e-3
PRICE_TOL = 1e-4
THEME_SEEDS: dict[str, tuple[str, ...]] = {
    "冰雪/寒潮供暖": ("冰", "雪", "冷", "寒", "供热", "供暖", "热力", "燃气"),
    "商业航天/卫星": ("航天", "卫星", "火箭", "星网", "遥感", "导航", "防务"),
    "影视/短剧传媒": ("影", "传媒", "文化", "广电", "出版", "影视", "数媒", "动漫"),
    "算力/PCB/光通信": ("电路", "光纤", "光通", "光讯", "算力", "光电", "数通"),
    "机器人/汽配智驾": ("机器人", "机电", "轴承", "减速", "精密", "传动", "智驾", "汽配"),
    "钨/战略小金属": ("钨", "稀土", "锑", "钼", "锆", "钛", "钴"),
}
THEME_INDUSTRIES: dict[str, tuple[str, ...]] = {
    "机器人/汽配智驾": ("汽车配件", "机械基件", "专用机械", "工程机械"),
    "影视/短剧传媒": ("影视音像", "出版业", "广告包装", "文教休闲"),
    "商业航天/卫星": ("航空", "航天国防"),
    "算力/PCB/光通信": ("通信设备",),
}
STOP_TOKENS = {
    "股份",
    "科技",
    "集团",
    "控股",
    "实业",
    "发展",
    "新材",
    "电子",
    "智能",
    "新能",
    "能源",
    "医药",
    "生物",
    "信息",
    "通信",
    "环保",
    "国际",
    "中国",
    "上海",
    "浙江",
    "江苏",
    "广东",
    "北京",
    "建设",
    "投资",
    "资本",
    "健康",
    "食品",
    "化工",
    "机械",
}
DRAGON_CORE_CHARS = frozenset("铝铜钨金银锂钴钛硅煤冰雪影药酒茶")


@dataclass(frozen=True)
class FiveMinSummary:
    first_5m_surge_pct: float
    first_5m_red: bool
    min_vol_ratio: float
    high_morning: float
    broke_morning_high: bool
    bars_since_high: int
    first_5m_low: float = 0.0
    held_first_5m_low: bool = True
    late_spike_fade: bool = False

    def format_tag(self) -> str:
        vol_str = f"缩量{self.min_vol_ratio * 100:.0f}%"
        if self.min_vol_ratio <= 0.15 and self.bars_since_high < 12:
            vol_str += "(<=15%锁仓)"
        mins = self.bars_since_high * 5
        if self.broke_morning_high:
            state = "过早高" if self.bars_since_high <= 2 else f"过早高|距高{mins}m"
        else:
            state = f"横盘{mins}m"
        if self.late_spike_fade:
            state += "|脉冲衰竭"
        low_tag = "守首K低" if self.held_first_5m_low else "破首K低"
        return f"[5m:首K{self.first_5m_surge_pct:+.1f}%|{low_tag}|{vol_str}|{state}]"


@dataclass(frozen=True)
class QuoteMetrics:
    symbol: str
    name: str
    prev_close: float
    open_price: float
    last_price: float
    high_price: float
    low_price: float
    limit_up_price: float
    vwap: float
    open_pct: float
    last_pct: float
    high_pct: float
    low_pct: float
    diff_from_open: float
    drop_from_high: float
    amplitude: float
    amount_yi: float
    is_limit_up: bool
    is_one_word: bool
    prev_boards: int = 0
    m_days_n_boards: str = ""
    industry: str = ""
    concepts: tuple[str, ...] = ()
    lu_reason: str = ""
    five_min: FiveMinSummary | None = None
    lu_60d: int | None = None
    lu_10d: int = 0
    circ_mv_yi: float = 0.0
    prev_turnover_pct: float = 0.0

    @property
    def has_monster_gene(self) -> bool:
        if self.is_limit_up or self.prev_boards >= 1 or bool(self.m_days_n_boards):
            return True
        if self.circ_mv_yi > 200.0:
            return self.lu_10d >= 2 and self.prev_turnover_pct >= 8.0
        if self.circ_mv_yi > 120.0 and (self.lu_10d < 1 or self.prev_turnover_pct < 5.0):
            return False
        intraday_tr = (self.amount_yi / self.circ_mv_yi * 100.0) if self.circ_mv_yi > 0 else 99.0
        if self.prev_turnover_pct > 0 and self.prev_turnover_pct < 2.5 and intraday_tr < 2.5:
            return False
        if self.lu_60d is not None and self.lu_60d < 1:
            return False
        return True

    @property
    def monster_tier(self) -> int:
        if not self.has_monster_gene:
            return 0
        if (self.lu_10d >= 1 or self.prev_boards >= 1 or bool(self.m_days_n_boards)) and (
            self.circ_mv_yi <= 0 or self.circ_mv_yi <= 150.0
        ):
            return 3
        if (self.lu_60d or 0) >= 2 and (self.circ_mv_yi <= 0 or self.circ_mv_yi <= 100.0):
            return 2
        return 1

    @property
    def above_vwap(self) -> bool:
        return self.last_price >= self.vwap - VWAP_TOL

    @property
    def rebound_from_low(self) -> float:
        return self.last_pct - self.low_pct

    @property
    def ladder_tier(self) -> int:
        return self.prev_boards if self.prev_boards >= 1 else (1 if self.m_days_n_boards else 0)

    @property
    def ladder_label(self) -> str:
        if self.prev_boards >= 1:
            base = f"{self.prev_boards}进{self.prev_boards + 1}"
            if self.is_limit_up:
                base += "晋级"
            return f"{base}({self.m_days_n_boards})" if self.m_days_n_boards else base
        if self.m_days_n_boards:
            return f"反包({self.m_days_n_boards})"
        return "首板封板" if self.is_limit_up else "首板候选"

    @property
    def reg_risk_tag(self) -> str:
        if self.prev_boards >= 5:
            return "⚡高标重度异动监管"
        if self.prev_boards >= 3 or any(k in self.m_days_n_boards for k in ("5板", "6板", "7板", "8板")):
            return "⚠️异动监管观察"
        return ""


def calc_limit_up_price(prev_close: float, symbol: str) -> float:
    ratio = Decimal("1.20") if symbol.startswith(("30", "68")) else Decimal("1.10")
    base = Decimal(f"{prev_close:.2f}")
    return float((base * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_quote(
    q: dict[str, Any],
    ladder_info: dict[str, dict[str, Any]] | None = None,
    industry_map: dict[str, str] | None = None,
    gene_map: dict[str, dict[str, float]] | None = None,
) -> QuoteMetrics | None:
    sym = str(q.get("symbol") or "")
    if not sym or sym.endswith(".BJ"):
        return None
    ext = q.get("ext") or {}
    name = str(ext.get("name") or "").strip()
    if not name or "ST" in name or name.startswith(("N", "C")):
        return None
    prev = float(q.get("prev_close") or 0.0)
    open_p = float(q.get("open") or 0.0)
    last_p = float(q.get("last_price") or 0.0)
    high_p = float(q.get("high") or 0.0)
    low_p = float(q.get("low") or 0.0)
    if prev <= 0 or open_p <= 0 or last_p <= 0:
        return None

    vol = float(q.get("volume") or 0.0)
    amt = float(q.get("amount") or 0.0)
    vwap = amt / vol / 100.0 if vol > 0 else last_p
    lu_price = calc_limit_up_price(prev, sym)
    is_lu = last_p >= lu_price - PRICE_TOL
    is_ow = is_lu and open_p >= lu_price - PRICE_TOL and low_p >= lu_price - PRICE_TOL
    open_pct = (open_p - prev) / prev * 100.0
    last_pct = (last_p - prev) / prev * 100.0
    high_pct = (high_p - prev) / prev * 100.0
    low_pct = (low_p - prev) / prev * 100.0
    info = (ladder_info or {}).get(sym) or {}
    ginfo = (gene_map or {}).get(sym) if gene_map is not None else None
    industry = (industry_map or {}).get(sym) or str(info.get("industry") or ext.get("industry") or "")

    return QuoteMetrics(
        symbol=sym,
        name=name,
        prev_close=prev,
        open_price=open_p,
        last_price=last_p,
        high_price=high_p,
        low_price=low_p,
        limit_up_price=lu_price,
        vwap=vwap,
        open_pct=open_pct,
        last_pct=last_pct,
        high_pct=high_pct,
        low_pct=low_pct,
        diff_from_open=last_pct - open_pct,
        drop_from_high=last_pct - high_pct,
        amplitude=(high_p - low_p) / prev * 100.0,
        amount_yi=amt / 1e8,
        is_limit_up=is_lu,
        is_one_word=is_ow,
        prev_boards=int(info.get("prev_boards") or 0),
        m_days_n_boards=str(info.get("m_days_n_boards") or ""),
        industry=industry,
        lu_60d=int(ginfo["lu_60d"]) if ginfo and "lu_60d" in ginfo else None,
        lu_10d=int(ginfo.get("lu_10d", 0)) if ginfo else 0,
        circ_mv_yi=float(ginfo.get("circ_mv_yi", 0.0)) if ginfo else 0.0,
        prev_turnover_pct=float(ginfo.get("turnover_pct", 0.0)) if ginfo else 0.0,
    )


def compute_ladder_from_daily_rows(
    rows_by_sym: dict[str, list[tuple[str, float, float]]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for sym, rows in rows_by_sym.items():
        if sym.endswith(".BJ") or not rows:
            continue
        ordered = sorted(rows, key=lambda x: x[0])[-20:]
        flags = [prev > 0 and close >= calc_limit_up_price(prev, sym) - PRICE_TOL for _, prev, close in ordered]
        consec = 0
        for is_lu in reversed(flags):
            if is_lu:
                consec += 1
            else:
                break
        win6 = flags[-6:]
        lu6 = sum(1 for f in win6 if f)
        win10 = flags[-10:]
        lu10 = sum(1 for f in win10 if f)
        if lu6 >= 3 and lu6 > consec:
            m_label = f"{len(win6)}天{lu6}板"
        elif lu10 >= 5 and lu10 > consec:
            m_label = f"{len(win10)}天{lu10}板"
        else:
            m_label = ""
        if consec >= 1 or m_label:
            result[sym] = {"prev_boards": consec, "m_days_n_boards": m_label}
    return result


def load_prev_ladder(snapshot_date: str, tushare_token: str | None) -> dict[str, dict[str, Any]]:
    cache_dir = Path.home() / ".wyckoff" / "cache"
    cache_file = cache_dir / f"prev_ladder_v2_{snapshot_date}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[WARN] 读取连板缓存失败 ({cache_file}): {exc}", file=sys.stderr)
    if not tushare_token:
        print("[WARN] TUSHARE_TOKEN 未配置，无法加载昨日连板梯队高度。", file=sys.stderr)
        return {}
    try:
        import tushare as ts

        pro = ts.pro_api(tushare_token)
        cal = pro.trade_cal(exchange="SSE", end_date=snapshot_date, is_open="1", limit=25)
        dates = sorted(d for d in cal["cal_date"].tolist() if d < snapshot_date)[-20:]
        if not dates:
            print(f"[WARN] 未获取到 {snapshot_date} 之前的交易日历，跳过连板计算。", file=sys.stderr)
            return {}
        rows_by_sym: dict[str, list[tuple[str, float, float]]] = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            dfs = list(
                ex.map(
                    lambda d: (d, pro.daily(trade_date=d, fields="ts_code,close,pre_close")),
                    dates,
                )
            )
        empty_dates = [d for d, df in dfs if df is None or df.empty]
        for d, df in dfs:
            if df is None or df.empty:
                continue
            for row in df.itertuples(index=False):
                rows_by_sym.setdefault(str(row.ts_code), []).append((d, float(row.pre_close), float(row.close)))
        ladder = compute_ladder_from_daily_rows(rows_by_sym)
        if empty_dates:
            print(f"[WARN] Tushare 日线存在空表日期 {empty_dates}，本次结果不写入缓存。", file=sys.stderr)
            return ladder
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(ladder, ensure_ascii=False), encoding="utf-8")
        return ladder
    except Exception as exc:
        print(f"[WARN] 拉取昨日连板梯队失败: {exc}", file=sys.stderr)
        return {}


def load_industry_map(tushare_token: str | None) -> dict[str, str]:
    cache_dir = Path.home() / ".wyckoff" / "cache"
    cache_file = cache_dir / "stock_industry_map.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and len(data) >= 1000:
                return {str(k): str(v) for k, v in data.items() if v}
        except Exception as exc:
            print(f"[WARN] 读取行业分类缓存失败 ({cache_file}): {exc}", file=sys.stderr)
    if not tushare_token:
        return {}
    try:
        import tushare as ts

        pro = ts.pro_api(tushare_token)
        df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,industry")
        if df is None or df.empty:
            return {}
        mapping = {str(r.ts_code): str(r.industry or "") for r in df.itertuples(index=False) if r.industry}
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        return mapping
    except Exception as exc:
        print(f"[WARN] 拉取 Tushare 行业分类失败: {exc}", file=sys.stderr)
        return {}


def compute_monster_gene_from_rows(
    rows_by_sym: dict[str, list[tuple[str, float, float]]],
    basic_by_sym: dict[str, tuple[float, float]] | None = None,
) -> dict[str, dict[str, float]]:
    bmap = basic_by_sym or {}
    all_syms = set(rows_by_sym.keys()) | set(bmap.keys())
    out: dict[str, dict[str, float]] = {}
    for sym in all_syms:
        if sym.endswith(".BJ"):
            continue
        ordered = sorted(rows_by_sym.get(sym, []), key=lambda x: x[0])[-60:]
        flags = [prev > 0 and close >= calc_limit_up_price(prev, sym) - PRICE_TOL for _, prev, close in ordered]
        cmv, tr = bmap.get(sym, (0.0, 0.0))
        out[sym] = {
            "lu_60d": sum(1 for f in flags if f),
            "lu_10d": sum(1 for f in flags[-10:] if f),
            "circ_mv_yi": round(cmv, 2),
            "turnover_pct": round(tr, 2),
        }
    return out


def load_monster_gene_map(snapshot_date: str, tushare_token: str | None) -> dict[str, dict[str, float]]:
    cache_dir = Path.home() / ".wyckoff" / "cache"
    cache_file = cache_dir / f"monster_gene_v1_{snapshot_date}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[WARN] 读取妖股基因缓存失败 ({cache_file}): {exc}", file=sys.stderr)
    if not tushare_token:
        return {}
    try:
        import tushare as ts

        pro = ts.pro_api(tushare_token)
        cal = pro.trade_cal(exchange="SSE", end_date=snapshot_date, is_open="1", limit=75)
        dates = sorted(d for d in cal["cal_date"].tolist() if d < snapshot_date)[-60:]
        if not dates:
            return {}
        rows_by_sym: dict[str, list[tuple[str, float, float]]] = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            dfs = list(ex.map(lambda d: (d, pro.daily(trade_date=d, fields="ts_code,close,pre_close")), dates))
        for d, df in dfs:
            if df is not None and not df.empty:
                for r in df.itertuples(index=False):
                    rows_by_sym.setdefault(str(r.ts_code), []).append((d, float(r.pre_close), float(r.close)))
        bdf = pro.daily_basic(trade_date=dates[-1], fields="ts_code,circ_mv,turnover_rate")
        basic_by_sym = {
            str(r.ts_code): (float(r.circ_mv or 0.0) / 10000.0, float(r.turnover_rate or 0.0))
            for r in (bdf.itertuples(index=False) if bdf is not None and not bdf.empty else [])
        }
        gene_map = compute_monster_gene_from_rows(rows_by_sym, basic_by_sym)
        if rows_by_sym and basic_by_sym:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(gene_map, ensure_ascii=False), encoding="utf-8")
        return gene_map
    except Exception as exc:
        print(f"[WARN] 拉取 60 日涨停基因与流通市值失败: {exc}", file=sys.stderr)
        return {}


def _detect_late_spike_fade(day_bars: list[dict[str, Any]], first_surge: float, prev_close: float) -> bool:
    if len(day_bars) < 24 or first_surge >= 1.8 or prev_close <= 0:
        return False
    amts = [float(b.get("amount") or b.get("volume") or 0.0) for b in day_bars]
    morning_slice = amts[1:16]
    morning_avg = sum(morning_slice) / len(morning_slice) if morning_slice else 0.0
    max_pm_amt = max(amts[16:], default=0.0)
    last_close = float(day_bars[-1].get("close") or 0.0)
    return (
        morning_avg > 0
        and max_pm_amt >= 4.0 * morning_avg
        and amts[-1] <= 0.60 * max_pm_amt
        and last_close < prev_close * 1.075
    )


def analyze_5m_bars(bars: Any, prev_close: float) -> FiveMinSummary | None:
    if bars is None or prev_close <= 0:
        return None
    records: list[dict[str, Any]] = bars.to_dict("records") if hasattr(bars, "to_dict") else list(bars)
    if not records:
        return None
    latest_date = str(records[-1].get("date") or records[-1].get("trade_date") or "")
    day_bars = (
        [b for b in records if str(b.get("date") or b.get("trade_date") or "") == latest_date]
        if latest_date
        else records
    )
    if not day_bars:
        return None
    b0 = day_bars[0]
    open_0 = float(b0.get("open") or 0.0)
    high_0 = float(b0.get("high") or 0.0)
    low_0 = float(b0.get("low") or 0.0)
    close_0 = float(b0.get("close") or 0.0)
    amt_0 = float(b0.get("amount") or b0.get("volume") or 0.0)
    first_surge = (high_0 - open_0) / prev_close * 100.0 if open_0 > 0 else 0.0
    consol_bars = [
        b
        for b in day_bars[1:6]
        if float(b.get("high") or 0.0) > float(b.get("low") or 0.0) + PRICE_TOL or float(b.get("open") or 0.0) <= 0.0
    ]
    subsequent_amts = [float(b.get("amount") or b.get("volume") or 0.0) for b in consol_bars]
    min_ratio = min(subsequent_amts) / amt_0 if (amt_0 > 0 and subsequent_amts) else 1.0
    high_morning = max(float(b.get("high") or 0.0) for b in day_bars[:3])
    highs = [float(b.get("high") or 0.0) for b in day_bars]
    max_high = max(highs)
    last_max_idx = max(idx for idx, h in enumerate(highs) if h >= max_high - PRICE_TOL)
    later_lows = [float(b.get("low") or 0.0) for b in day_bars[1:] if float(b.get("low") or 0.0) > 0]
    held_low = low_0 <= 0 or min(later_lows, default=low_0) >= low_0 - PRICE_TOL
    return FiveMinSummary(
        first_5m_surge_pct=first_surge,
        first_5m_red=close_0 >= prev_close - PRICE_TOL,
        min_vol_ratio=min_ratio,
        high_morning=high_morning,
        broke_morning_high=float(day_bars[-1].get("close") or 0.0) >= high_morning - PRICE_TOL,
        bars_since_high=len(day_bars) - 1 - last_max_idx,
        first_5m_low=low_0,
        held_first_5m_low=held_low,
        late_spike_fade=_detect_late_spike_fade(day_bars, first_surge, prev_close),
    )


def extract_snapshot_time(quotes: Iterable[dict[str, Any]], fallback_now: datetime | None = None) -> datetime:
    max_ts = max((int(q.get("timestamp") or 0) for q in quotes), default=0)
    if max_ts > 0:
        return datetime.fromtimestamp(max_ts / 1000.0, tz=SH_TZ)
    return fallback_now or datetime.now(SH_TZ)


def _is_ladder_reversal(m: QuoteMetrics) -> bool:
    if not m.above_vwap or m.last_pct < 0.0:
        return False
    if m.open_pct <= -2.5 and m.diff_from_open >= 6.0:
        return True
    if m.low_pct <= -3.0 and m.rebound_from_low >= 7.0 and m.last_pct >= 1.5:
        return True
    return m.prev_boards == 0 and bool(m.m_days_n_boards) and m.open_pct <= 1.0 and m.diff_from_open >= 6.0


def _is_first_board_reversal(m: QuoteMetrics) -> bool:
    if m.is_limit_up or m.prev_boards > 0 or m.m_days_n_boards or not m.above_vwap or m.amount_yi < 1.0:
        return False
    is_20cm = m.symbol.startswith(("30", "68"))
    min_surge = 11.0 if is_20cm else 6.0
    min_rebound = 13.0 if is_20cm else 8.0
    min_last = 5.0 if is_20cm else 2.0
    if m.last_pct < min_last:
        return False
    return (m.open_pct <= -2.5 and m.diff_from_open >= min_surge) or (
        m.low_pct <= -3.0 and m.rebound_from_low >= min_rebound
    )


def _passes_price_cap(m: QuoteMetrics, max_price: float | None) -> bool:
    return max_price is None or m.last_price <= max_price + PRICE_TOL


def classify_ladder_buckets(
    metrics: list[QuoteMetrics],
    keyword: str | None = None,
    max_price: float | None = None,
) -> dict[str, list[QuoteMetrics]]:
    prev_pool = sorted(
        [m for m in metrics if m.prev_boards >= 1 or m.m_days_n_boards],
        key=lambda x: (x.ladder_tier, x.last_pct, bool(x.m_days_n_boards)),
        reverse=True,
    )
    one_words = sorted([m for m in metrics if m.is_one_word], key=lambda x: (x.ladder_tier, x.amount_yi), reverse=True)
    turnover_lu = sorted(
        [m for m in metrics if m.is_limit_up and not m.is_one_word],
        key=lambda x: (x.ladder_tier, x.diff_from_open),
        reverse=True,
    )
    ladder_reversals = sorted(
        [m for m in prev_pool if _is_ladder_reversal(m) and _passes_price_cap(m, max_price)],
        key=lambda x: (x.ladder_tier, max(x.diff_from_open, x.rebound_from_low)),
        reverse=True,
    )
    first_reversals = sorted(
        [m for m in metrics if _is_first_board_reversal(m) and _passes_price_cap(m, max_price)],
        key=lambda x: max(x.diff_from_open, x.rebound_from_low),
        reverse=True,
    )
    traps = sorted(
        [
            m
            for m in metrics
            if not m.is_limit_up and m.high_pct >= 6.0 and m.drop_from_high <= -4.5 and not m.above_vwap
        ],
        key=lambda x: (x.ladder_tier, -x.drop_from_high),
        reverse=True,
    )
    kw_matches = (
        sorted(
            [m for m in metrics if keyword in m.name and _passes_price_cap(m, max_price)],
            key=lambda x: x.last_pct,
            reverse=True,
        )
        if keyword
        else []
    )
    return {
        "prev_ladder_pool": prev_pool,
        "one_words": one_words,
        "turnover_lu": turnover_lu,
        "ladder_reversals": ladder_reversals,
        "deep_reversals": ladder_reversals + first_reversals,
        "traps": traps,
        "keyword_matches": kw_matches,
    }


def reversal_risk_tag(m: QuoteMetrics) -> str:
    if m.is_limit_up:
        return ""
    bars_high = m.five_min.bars_since_high if m.five_min else 0
    if m.high_pct >= 8.0 and (m.rebound_from_low >= 9.0 or m.ladder_tier >= 1 or m.high_pct >= 9.2):
        if m.drop_from_high <= -4.5:
            return "[🚨冲板失败大幅回落·做T砸盘兑现]"
        if bars_high >= 15:
            return "[🚨摸高久盘不封(>75m)·防尾盘做T砸盘]"
        if bars_high >= 6 or m.drop_from_high <= -2.0:
            return "[⚠️冲板未封回落·禁半山腰追高]"
    if m.five_min and m.five_min.late_spike_fade:
        return "[⚠️午后量化脉冲衰竭·防冲高回落]"
    if m.above_vwap and 1.5 <= m.last_pct <= 5.5 and m.vwap > 0 and (m.last_price - m.vwap) / m.vwap <= 0.025:
        return "[✅贴均线弱转强低吸区]"
    return ""


def tag_theme_front_runner(m: QuoteMetrics) -> str:
    risk = reversal_risk_tag(m)
    if "🚨" in risk or "⚠️" in risk:
        return risk
    if not m.has_monster_gene:
        return "[🧊缺妖气(60日0板/大盘低换手)·勿选]"
    near_vwap = m.vwap > 0 and (m.last_price - m.vwap) / m.vwap <= 0.025
    held_low = m.five_min.held_first_5m_low if m.five_min else True
    if 3.0 <= m.last_pct <= 6.8 and near_vwap and held_low:
        if m.monster_tier >= 3:
            return "[🎯主线妖股苗子·N字基因贴均线守首K]"
        return "[🎯主线低吸首选·贴均线守首K]"
    if m.last_pct >= 6.8 and held_low:
        return "[⚡主线前排冲板博弈]"
    return "[⏸️同簇跟风观察(破首K低或偏离均线)]"


def tag_keyword_candidate(m: QuoteMetrics, front_lu_count: int, now_hhmm: str) -> str:
    if m.is_limit_up:
        return "[🔥前排封板先锋]"
    if not m.above_vwap and m.drop_from_high <= -4.5:
        return "[❌冲高破均线观察]"
    if front_lu_count >= 4 and now_hhmm >= "10:30" and 0.0 <= m.last_pct <= 2.5:
        return "[⚠️10:30后平盘观察]"
    if m.above_vwap and m.last_pct >= 3.0:
        return "[✅均线上活跃前排]"
    return "[⏸️后排跟风观察]"


def format_row(m: QuoteMetrics, extra_tag: str = "") -> str:
    vwap_tag = ">=VWAP" if m.above_vwap else "<VWAP"
    five_tag = f" {m.five_min.format_tag()}" if m.five_min else ""
    gene_parts: list[str] = []
    if m.circ_mv_yi > 0:
        gene_parts.append(f"盘{m.circ_mv_yi:.0f}亿")
    if m.lu_60d is not None:
        gene_parts.append(f"60d:{m.lu_60d}板" + (f"/10d:{m.lu_10d}板" if m.lu_10d > 0 else ""))
    gene_tag = f" [{'|'.join(gene_parts)}]" if gene_parts else ""
    reg_tag = f" [{m.reg_risk_tag}]" if m.reg_risk_tag else ""
    rsn_tag = f" [归因:{m.lu_reason}]" if m.lu_reason else ""
    suffix = f" {extra_tag}" if extra_tag else ""
    return (
        f"  {m.symbol:10s} {m.name:8s} [{m.ladder_label:8s}] | Open:{m.open_pct:+6.2f}% Low:{m.low_pct:+6.2f}% "
        f"-> Now:{m.last_pct:+6.2f}% ({m.last_price:6.2f}, {vwap_tag}:{m.vwap:6.2f}) "
        f"| High:{m.high_pct:+6.2f}% | FromOpen:{m.diff_from_open:+6.2f}% | Amt:{m.amount_yi:5.2f}亿{gene_tag}{five_tag}{reg_tag}{rsn_tag}{suffix}"
    )


_CONCEPT_META_CACHE: dict[str, dict[str, Any]] = {}


def _format_concept_meta(info: dict[str, Any]) -> str:
    if not info:
        return ""
    parts: list[str] = []
    if info.get("order"):
        ht = f"·{info['hot_tag']}" if info.get("hot_tag") else ""
        parts.append(f"同花顺热榜#{info['order']}{ht}")
    elif info.get("source"):
        parts.append(str(info["source"]))
    if info.get("high"):
        parts.append(f"最高标:{info['high']}")
    if info.get("up"):
        parts.append(f"东财红盘援军:{info['up']}家")
    return f" [{' | '.join(parts)}]" if parts else ""


def matches_theme_cluster(m: QuoteMetrics, theme_name: str) -> bool:
    if theme_name in m.concepts:
        return True
    if theme_name in THEME_SEEDS:
        tokens = THEME_SEEDS[theme_name]
        inds = THEME_INDUSTRIES.get(theme_name, ())
        return any(t in m.name for t in tokens) or (bool(m.industry) and m.industry in inds)
    if theme_name.startswith("行业:"):
        return bool(m.industry) and m.industry == theme_name.removeprefix("行业:")
    if theme_name.startswith("『") and theme_name.endswith("』字辈"):
        tok = theme_name[1:-3]
        return bool(tok) and tok in m.name
    return False


def _build_seed_theme_groups(strong: list[QuoteMetrics]) -> dict[str, list[QuoteMetrics]]:
    groups: dict[str, list[QuoteMetrics]] = {}
    for theme_name in THEME_SEEDS:
        matched = [m for m in strong if matches_theme_cluster(m, theme_name)]
        if matched:
            groups[theme_name] = matched
    merged_inds = {ind for inds in THEME_INDUSTRIES.values() for ind in inds}
    for m in strong:
        if m.industry and m.industry not in merged_inds:
            groups.setdefault(f"行业:{m.industry}", []).append(m)
    return groups


def summarize_promotion_battle(prev_pool: list[QuoteMetrics]) -> list[str]:
    if not prev_pool:
        return []
    tiers: dict[str, list[QuoteMetrics]] = {}
    high_boards = sorted({m.prev_boards for m in prev_pool if m.prev_boards >= 2}, reverse=True)
    for b in high_boards:
        tiers[f"{b}进{b + 1}"] = [m for m in prev_pool if m.prev_boards == b]
    if any(m.prev_boards == 1 for m in prev_pool):
        tiers["1进2"] = [m for m in prev_pool if m.prev_boards == 1]
    if any(m.prev_boards == 0 and m.m_days_n_boards for m in prev_pool):
        tiers["断板反包"] = [m for m in prev_pool if m.prev_boards == 0 and m.m_days_n_boards]
    lines: list[str] = []
    for label, group in tiers.items():
        prom = [m for m in group if m.is_limit_up]
        fight = [m for m in group if not m.is_limit_up and m.above_vwap and m.last_pct >= 2.0]
        blown = [m for m in group if not m.is_limit_up and m.high_pct >= 8.0 and m.drop_from_high <= -4.0]
        elim = [m for m in group if m.last_pct <= -2.0 or (not m.above_vwap and m.last_pct < 0)]
        rate = int(round(len(prom) / len(group) * 100))
        p_names = f": {'/'.join(m.name for m in prom[:4])}" if prom else ""
        b_names = f"({'/'.join(m.name for m in blown[:3])})" if blown else ""
        lines.append(
            f"⚔️ {label}({len(group)}只): 晋级{len(prom)}只({rate}%{p_names}) | 均线上待晋{len(fight)}只 "
            f"| 冲板派面{len(blown)}只{b_names} | 绿盘掉队{len(elim)}只"
        )
    return lines


def _evaluate_theme_echelon(
    name: str,
    sealed: list[QuoteMetrics],
    all_metrics: list[QuoteMetrics],
) -> dict[str, Any]:
    ladder_peers = [
        m for m in all_metrics if (m.prev_boards >= 1 or bool(m.m_days_n_boards)) and matches_theme_cluster(m, name)
    ]
    prom_cnt = sum(1 for m in ladder_peers if m.is_limit_up)
    casualties = [
        m
        for m in all_metrics
        if matches_theme_cluster(m, name)
        and not m.is_limit_up
        and (
            ((m.prev_boards >= 1 or bool(m.m_days_n_boards)) and m.last_pct <= -2.5)
            or (m.high_pct >= 7.5 and m.drop_from_high <= -4.0 and not m.above_vwap)
        )
    ]
    heights = sorted({(m.prev_boards + 1 if m.prev_boards >= 1 else 1) for m in sealed}, reverse=True)
    t_str = "-".join(str(h) for h in heights) + "板" if heights else "0板"
    cas_n = len(casualties)
    if prom_cnt >= 2 and cas_n <= prom_cnt:
        score = 2
        tag = f"[🏆梯队完整({t_str})·晋级{prom_cnt}/{len(ladder_peers)}·小弟正反馈]"
    elif cas_n >= 3 and cas_n > max(prom_cnt, 1) * 1.5:
        score = -1
        bad_str = "/".join(f"{m.name}{m.last_pct:+.1f}%" for m in sorted(casualties, key=lambda x: x.last_pct)[:3])
        tag = f"[⚠️前排独食/后排派面·掉队{cas_n}只({bad_str})·慎追后排]"
    else:
        score = 0
        tag = f"[⚖️梯队({t_str})·晋级{prom_cnt}/{len(ladder_peers)}·后排掉队{cas_n}只]"
    return {"echelon_score": score, "echelon_tag": tag, "promoted_cnt": prom_cnt, "backrow_drop_cnt": cas_n}


def detect_fermenting_themes(metrics: list[QuoteMetrics]) -> list[dict[str, Any]]:
    strong = [m for m in metrics if m.is_limit_up or (m.above_vwap and m.last_pct >= 3.5)]
    has_rt_concepts = any(m.concepts for m in strong)
    groups: dict[str, list[QuoteMetrics]] = {}
    if has_rt_concepts:
        raw_c: dict[str, list[QuoteMetrics]] = {}
        for m in strong:
            for c in m.concepts:
                raw_c.setdefault(c, []).append(m)
        for cname, members in raw_c.items():
            lu_n = sum(1 for x in members if x.is_limit_up)
            if cname in _CONCEPT_META_CACHE or lu_n >= 3:
                groups[cname] = members
    else:
        groups = _build_seed_theme_groups(strong)

    seed_flat = {t for tokens in THEME_SEEDS.values() for t in tokens}
    token_counts: dict[str, list[QuoteMetrics]] = {}
    for m in strong:
        seen = {m.name[i : i + 2] for i in range(len(m.name) - 1)}
        for tok in seen:
            if tok not in STOP_TOKENS and (has_rt_concepts or tok not in seed_flat):
                token_counts.setdefault(tok, []).append(m)
    for tok, members in token_counts.items():
        if len(members) >= 3:
            groups[f"『{tok}』字辈"] = members

    results: list[dict[str, Any]] = []
    for name, members in groups.items():
        sealed = sorted([m for m in members if m.is_limit_up], key=lambda x: (x.ladder_tier, x.amount_yi), reverse=True)
        active = sorted(
            [m for m in members if not m.is_limit_up and m.above_vwap and m.last_pct >= 3.5],
            key=lambda x: (x.last_pct, x.amount_yi),
            reverse=True,
        )
        if len(sealed) >= 2 or (len(sealed) >= 1 and len(members) >= 4):
            meta_info = _CONCEPT_META_CACHE.get(name, {})
            ech = _evaluate_theme_echelon(name, sealed, metrics)
            results.append(
                {
                    "theme": name,
                    "meta_tag": _format_concept_meta(meta_info),
                    "hot_rank": int(meta_info.get("order") or 99),
                    "lu_count": len(sealed),
                    "total_strong": len(members),
                    "sealed": sealed,
                    "active_front": active,
                    **ech,
                }
            )
    return sorted(
        results, key=lambda r: (r["lu_count"], r["echelon_score"], -r["hot_rank"], r["total_strong"]), reverse=True
    )


def parse_holdings_arg(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    out: dict[str, float] = {}
    for item in raw.split(","):
        part = item.strip()
        if not part:
            continue
        if ":" in part:
            sym, cost_str = part.split(":", 1)
            norm = normalize_cn_symbol(sym) or sym.strip().upper()
            out[norm] = float(cost_str)
        else:
            norm = normalize_cn_symbol(part) or part.upper()
            out[norm] = 0.0
    return out


def _holding_action_tag(m: QuoteMetrics, matched_themes: list[str]) -> str:
    if m.is_limit_up:
        return "[🔥涨停封死·锁仓持有]"
    risk = reversal_risk_tag(m)
    if "🚨" in risk or "⚠️" in risk:
        return risk
    held_low = m.five_min.held_first_5m_low if m.five_min else (m.low_pct >= m.open_pct - 0.8)
    if not m.above_vwap:
        is_multi_board = m.prev_boards >= 2 or bool(m.m_days_n_boards)
        shrunk = m.five_min is not None and m.five_min.min_vol_ratio <= 0.30
        if is_multi_board and shrunk and m.last_pct > -6.0:
            return "[🛡️连板分歧缩量换手·宜分批锁利留底仓看均线承接]"
        if not held_low or m.last_pct <= -6.0:
            return "[🚨破VWAP且破首K低·果断去弱留强]"
        return "[⚠️跌破VWAP·反抽不站回则减仓]"
    if matched_themes and held_low and m.last_pct >= 1.5:
        return f"[🛡️主线共振({'/'.join(matched_themes[:2])})+守首K低·格局防卖飞]"
    if held_low and m.last_pct < 1.5:
        return "[⏸️平盘横盘观察·不盲目补仓]"
    if held_low:
        return "[✅站稳VWAP且守首K低·沿均线持有]"
    return "[⏸️站上VWAP但曾破首K低·冲高分批锁利]"


def print_holdings_section(metrics: list[QuoteMetrics], holdings: dict[str, float]) -> None:
    if not holdings:
        return
    themes = detect_fermenting_themes(metrics)
    by_sym = {m.symbol: m for m in metrics}
    print(f"=== [H] 持仓股主线共振与去弱留强诊断 ({len(holdings)}只) ===")
    for sym, cost in holdings.items():
        m = by_sym.get(sym)
        if not m:
            continue
        matched = [
            f"{t['theme']}({t['lu_count']}板)"
            for t in themes
            if any(x.symbol == sym for x in t["sealed"] + t["active_front"]) or matches_theme_cluster(m, t["theme"])
        ]
        pnl_tag = f"PnL:{(m.last_price / cost - 1) * 100:+.2f}% " if cost > 0 else ""
        print(format_row(m, f"{pnl_tag}{_holding_action_tag(m, matched)}"))
    print()


def _dragon_root_tokens(dragon: QuoteMetrics) -> set[str]:
    roots = {ch for ch in dragon.name if ch in DRAGON_CORE_CHARS}
    for i in range(len(dragon.name) - 1):
        tok = dragon.name[i : i + 2]
        if tok not in STOP_TOKENS:
            roots.add(tok)
    return roots


def find_dragon_companions(
    metrics: list[QuoteMetrics],
    max_price: float | None = None,
    limit: int = 6,
) -> list[tuple[QuoteMetrics, list[QuoteMetrics]]]:
    dragons = sorted(
        [m for m in metrics if m.prev_boards >= 3 and (m.is_limit_up or (m.above_vwap and m.last_pct >= 4.0))],
        key=lambda x: (x.prev_boards, x.last_pct),
        reverse=True,
    )[:3]
    price_cap = max_price if max_price is not None else 15.0
    results: list[tuple[QuoteMetrics, list[QuoteMetrics]]] = []
    for d in dragons:
        roots = _dragon_root_tokens(d)
        companions: list[QuoteMetrics] = []
        for m in metrics:
            if m.symbol == d.symbol or m.symbol.startswith(("30", "68")):
                continue
            if m.prev_boards > 0 or m.m_days_n_boards or m.last_price > price_cap + PRICE_TOL:
                continue
            if not m.above_vwap or m.last_pct < 0.0 or m.amount_yi < 0.8 or not m.has_monster_gene:
                continue
            name_hit = any(r in m.name for r in roots)
            ind_hit = bool(m.industry and any(r in m.industry for r in roots if r in DRAGON_CORE_CHARS))
            if not (name_hit or ind_hit):
                continue
            if m.five_min and not m.is_limit_up:
                if not m.five_min.held_first_5m_low or (m.five_min.first_5m_surge_pct < 2.5 and m.last_pct < 3.0):
                    continue
            companions.append(m)
        companions.sort(
            key=lambda x: (
                x.is_limit_up,
                x.last_price <= 10.0,
                x.five_min.first_5m_surge_pct if x.five_min else x.high_pct,
                x.last_pct,
            ),
            reverse=True,
        )
        if companions:
            results.append((d, companions[:limit]))
    return results


def tag_dragon_companion(c: QuoteMetrics, dragon: QuoteMetrics) -> str:
    p_tag = "<=10元" if c.last_price <= 10.0 else "<=15元"
    if c.is_limit_up:
        return f"[🔥伴生{dragon.name[:2]}·首板封死(0异动/{p_tag})]"
    if (
        c.five_min
        and c.five_min.held_first_5m_low
        and c.five_min.min_vol_ratio <= 0.15
        and c.five_min.first_5m_surge_pct >= 2.5
    ):
        return f"[🎯伴生{dragon.name[:2]}·首K试盘锁仓潜伏(0异动/{p_tag})]"
    return f"[👀伴生{dragon.name[:2]}·均线上方观察(0异动/{p_tag})]"


def print_dragon_companions(metrics: list[QuoteMetrics], max_price: float | None = None) -> None:
    pairs = find_dragon_companions(metrics, max_price=max_price)
    if not pairs:
        return
    print(f"\n=== [B] 最高板/空间龙同宗低价伴生小弟雷达 (0异动低位补涨, 股价<={max_price or 15.0:.0f}元) ===")
    for dragon, companions in pairs:
        reg = f" | {dragon.reg_risk_tag}" if dragon.reg_risk_tag else ""
        print(
            f"  🐉 空间龙锚点: {dragon.symbol} {dragon.name} [{dragon.ladder_label}] Now:{dragon.last_pct:+.2f}% ({dragon.last_price:.2f}元){reg}"
        )
        for c in companions:
            print("    ->" + format_row(c, tag_dragon_companion(c, dragon)))


def _select_theme_display_candidates(
    active: list[QuoteMetrics],
    dip_limit: int = 2,
    max_price: float | None = None,
) -> list[QuoteMetrics]:
    filtered = [m for m in active if _passes_price_cap(m, max_price) and m.has_monster_gene]
    top_front = filtered[:3]
    seen = {m.symbol for m in top_front}
    dip_picks = sorted(
        [m for m in filtered if m.symbol not in seen and "🎯" in tag_theme_front_runner(m) and m.amount_yi >= 0.8],
        key=lambda x: (-x.monster_tier, 0 if x.last_price <= 10.0 else (1 if x.last_price <= 15.0 else 2), -x.last_pct),
    )[:dip_limit]
    return top_front + dip_picks


def print_theme_resonance(metrics: list[QuoteMetrics], max_price: float | None = None) -> None:
    themes = detect_fermenting_themes(metrics)
    print(f"\n=== [5] 盘中题材/字根共振发酵雷达 (同花顺+东财实时热点下钻成分股: {len(themes)}个主线簇) ===")
    for item in themes[:8]:
        lu_names = "、".join(f"{m.name}[{m.ladder_label}]" for m in item["sealed"][:6])
        meta_str = item.get("meta_tag") or ""
        ech_str = f" {item['echelon_tag']}" if item.get("echelon_tag") else ""
        print(
            f"  🔥 【{item['theme']}】{meta_str}{ech_str} (共振强股 {item['total_strong']}只 | 已涨停 {item['lu_count']}只: {lu_names})"
        )
        for m in _select_theme_display_candidates(item["active_front"], max_price=max_price):
            print("    ->" + format_row(m, tag_theme_front_runner(m)))


def print_sections(
    metrics: list[QuoteMetrics],
    keyword: str | None,
    now_hhmm: str,
    holdings: dict[str, float] | None = None,
    max_price: float | None = None,
) -> None:
    print_holdings_section(metrics, holdings or {})
    buckets = classify_ladder_buckets(metrics, keyword, max_price=max_price)
    if buckets["prev_ladder_pool"]:
        print(
            f"=== [0] 昨日涨停/连板池今日晋级PK记分牌 & 全量监控 (100%保留不因低开剔除: {len(buckets['prev_ladder_pool'])}只) ==="
        )
        for pk_line in summarize_promotion_battle(buckets["prev_ladder_pool"]):
            print(f"  {pk_line}")
        print("  ---")
        for m in buckets["prev_ladder_pool"]:
            print(format_row(m, reversal_risk_tag(m)))
        print()

    print(f"=== [1] 一字涨停顶死 ({len(buckets['one_words'])}只) ===")
    for m in buckets["one_words"]:
        print(format_row(m))

    print(f"\n=== [2] 换手真封死涨停 ({len(buckets['turnover_lu'])}只, 按连板高度与弱转强幅度排序) ===")
    for m in buckets["turnover_lu"]:
        print(format_row(m))

    print(
        f"\n=== [3] 深水穿云箭/大长腿与首阴反包观察 (竞价<=-2.5%或盘中Low<=-3%深水拉升/高标反包, 站稳VWAP: {len(buckets['deep_reversals'])}只) ==="
    )
    for m in buckets["deep_reversals"][:25]:
        print(format_row(m, reversal_risk_tag(m)))

    print(f"\n=== [4] 冲高回落破VWAP观察名单 (摸高>=+6%后回撤>=4.5%且破均线: {len(buckets['traps'])}只) ===")
    for m in buckets["traps"][:20]:
        print(format_row(m))

    print_dragon_companions(metrics, max_price=max_price)
    print_theme_resonance(metrics, max_price=max_price)

    if keyword:
        kw_list = buckets["keyword_matches"]
        lu_cnt = sum(1 for m in kw_list if m.is_limit_up)
        print(f"\n=== [6] 关键词『{keyword}』字辈/主题全扫描 ({len(kw_list)}只, 涨停{lu_cnt}只) ===")
        for m in kw_list:
            print(format_row(m, tag_keyword_candidate(m, lu_cnt, now_hhmm)))


def enrich_with_realtime_concepts(
    metrics: list[QuoteMetrics],
    holding_syms: Iterable[str] = (),
) -> list[QuoteMetrics]:
    hold_set = set(holding_syms)
    target_syms = [
        m.symbol
        for m in metrics
        if m.is_limit_up
        or m.prev_boards >= 1
        or bool(m.m_days_n_boards)
        or (m.above_vwap and m.last_pct >= 2.5)
        or m.symbol in hold_set
    ]
    meta, stk_map, reasons = fetch_realtime_concept_bundle(target_syms)
    _CONCEPT_META_CACHE.clear()
    _CONCEPT_META_CACHE.update(meta)
    enriched: list[QuoteMetrics] = []
    for m in metrics:
        code6 = m.symbol[:6]
        em_ind, concepts = stk_map.get(code6, ("", ()))
        rsn = reasons.get(code6, "")
        if concepts or rsn or em_ind:
            enriched.append(
                replace(
                    m,
                    industry=m.industry or em_ind,
                    concepts=concepts,
                    lu_reason=rsn,
                )
            )
        else:
            enriched.append(m)
    return enriched


def enrich_with_5m_bars(
    client: TickFlowClient,
    metrics: list[QuoteMetrics],
    holding_syms: Iterable[str] = (),
    max_price: float | None = None,
) -> list[QuoteMetrics]:
    buckets = classify_ladder_buckets(metrics, max_price=max_price)
    theme_fronts = [
        m
        for t in detect_fermenting_themes(metrics)[:8]
        for m in (
            t["active_front"][:6]
            + _select_theme_display_candidates(t["active_front"], dip_limit=5, max_price=max_price)
        )
    ]
    companion_items = [c for _, comps in find_dragon_companions(metrics, max_price=max_price, limit=15) for c in comps]
    sniper_pool = sorted(
        [
            m
            for m in metrics
            if not m.symbol.startswith(("30", "68"))
            and m.above_vwap
            and 2.5 <= m.last_pct <= 7.2
            and m.amount_yi >= 0.6
            and m.has_monster_gene
            and _passes_price_cap(m, max_price)
            and m.concepts
        ],
        key=lambda x: (-x.monster_tier, x.last_price > 10.0, -x.last_pct),
    )[:30]
    hold_set = set(holding_syms)
    hold_items = [m for m in metrics if m.symbol in hold_set]
    targets = {
        m.symbol: m
        for m in (
            hold_items
            + buckets["prev_ladder_pool"]
            + buckets["deep_reversals"][:25]
            + buckets["traps"][:15]
            + theme_fronts
            + companion_items
            + sniper_pool
        )
    }
    if not targets:
        return metrics
    syms = list(targets.keys())
    bars_map: dict[str, Any] = {}
    for i in range(0, len(syms), 45):
        try:
            batch = client.get_intraday_batch(syms[i : i + 45], period="5m", count=55)
            if isinstance(batch, dict):
                bars_map.update(batch)
        except Exception:
            continue
    if not bars_map:
        return metrics
    enriched: list[QuoteMetrics] = []
    for m in metrics:
        bars = bars_map.get(m.symbol)
        summary = analyze_5m_bars(bars, m.prev_close) if bars is not None else None
        enriched.append(replace(m, five_min=summary) if summary else m)
    return enriched


def _run_sniper_and_feishu(
    parsed: list[QuoteMetrics],
    snap_dt: datetime,
    ts_token: str | None,
    max_price: float | None,
    feishu_notify: bool,
    feishu_level: str,
    dry_run_feishu: bool,
) -> None:
    sentiment = compute_market_sentiment(parsed)
    print(f"\n=== [S] 全市场派面温度计 & 60日套牢排雷狙击精选 ===\n  🌡️ {sentiment.format_banner()}")
    themes = detect_fermenting_themes(parsed)
    pre_cands = select_sniper_targets(
        parsed,
        themes,
        sentiment,
        snap_dt.strftime("%H:%M"),
        max_price=max_price or 15.0,
        reversal_risk_fn=reversal_risk_tag,
    )
    if not pre_cands:
        print("  ⏸️ 当前无满足「主线共振 + 贴均线守首K + 抗派面分歧」的高胜率狙击标的，空仓等待右侧信号。")
        return
    overhead_map = fetch_daily_overhead_map([c.symbol for c in pre_cands], snap_dt.strftime("%Y%m%d"), ts_token)
    final_cands = select_sniper_targets(
        parsed,
        themes,
        sentiment,
        snap_dt.strftime("%H:%M"),
        overhead_rows=overhead_map,
        max_price=max_price or 15.0,
        reversal_risk_fn=reversal_risk_tag,
    )
    if not final_cands:
        print("  ⏸️ 初筛候选均因「近15~60日上方套牢盘厚重」被排雷剔除，继续空仓等待。")
        return
    for c in final_cands[:3]:
        print(
            f"  🎯 {c.symbol} {c.name:<6} 现价:{c.last_price:6.2f}元({c.last_pct:+5.2f}%) "
            f"| 目标低吸:{c.target_buy_low:.2f}~{c.target_buy_high:.2f}元 | 防守:{c.defense_price:.2f}元 "
            f"| 冲板目标:{c.limit_up_price:.2f}元 | {c.daily_tag} | 主线:{'+'.join(c.themes[:3])}"
        )
    if feishu_notify or dry_run_feishu:
        prev_pool = classify_ladder_buckets(parsed)["prev_ladder_pool"]
        push_sniper_feishu_alert(
            final_cands,
            sentiment,
            snap_dt.strftime("%Y%m%d"),
            snap_dt.strftime("%Y-%m-%d %H:%M:%S"),
            level=feishu_level,
            dry_run=dry_run_feishu,
            ladder_brief=summarize_promotion_battle(prev_pool),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan A-share limit-up ladder and reversals.")
    parser.add_argument("--keyword", type=str, default=None, help="Filter prefix/theme keyword.")
    parser.add_argument("--holdings", type=str, default=None, help="Comma-separated holdings (e.g. 600001.SH:10.00).")
    parser.add_argument("--max-price", type=float, default=None, help="Max stock price filter (e.g. 15.0 or 10.0).")
    parser.add_argument("--no-5m", action="store_true", help="Skip 5-minute K-line verification.")
    parser.add_argument("--feishu-notify", action="store_true", help="Push newly discovered sniper targets to Feishu.")
    parser.add_argument("--feishu-level", type=str, default="L2", choices=["L1", "L2", "L3"], help="Feishu alert tier.")
    parser.add_argument("--dry-run-feishu", action="store_true", help="Preview Feishu sniper card without sending.")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("TICKFLOW_API_KEY", "")
    if not api_key:
        raise RuntimeError("Missing TICKFLOW_API_KEY in environment.")

    holdings = parse_holdings_arg(args.holdings)
    client = TickFlowClient(api_key)
    qmap = client.get_quotes(universes=["CN_Equity_A"])
    snap_dt = extract_snapshot_time(qmap.values())
    ts_token = os.environ.get("TUSHARE_TOKEN")
    snap_ymd = snap_dt.strftime("%Y%m%d")
    ladder_info = load_prev_ladder(snap_ymd, ts_token)
    industry_map = load_industry_map(ts_token)
    gene_map = load_monster_gene_map(snap_ymd, ts_token)
    parsed = [m for q in qmap.values() if (m := parse_quote(q, ladder_info, industry_map, gene_map)) is not None]
    parsed = enrich_with_realtime_concepts(parsed, holdings.keys())
    if not args.no_5m:
        parsed = enrich_with_5m_bars(client, parsed, holdings.keys(), max_price=args.max_price)
    print(
        f"Snapshot Time (Asia/Shanghai): {snap_dt.strftime('%Y-%m-%d %H:%M:%S')} "
        f"| Valid Non-ST Main/ChiNext/STAR A-Shares: {len(parsed)} | Prev Ladder Pool: {len(ladder_info)}\n"
    )
    print_sections(parsed, args.keyword, snap_dt.strftime("%H:%M"), holdings, max_price=args.max_price)
    _run_sniper_and_feishu(
        parsed,
        snap_dt,
        ts_token,
        args.max_price,
        args.feishu_notify,
        args.feishu_level,
        args.dry_run_feishu,
    )


if __name__ == "__main__":
    main()
