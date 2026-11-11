#!/usr/bin/env python3
"""Personal A-share trade review: fills -> round trips -> attribution -> exit-rule replay.

Inputs are the user's own fills (never committed). Outputs go to a directory outside the repo.
Daily bars are fetched from TickFlow with ``adjust="none"`` so they line up with real fill prices.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

INDEX_CODES = ("000300.SH", "000852.SH")
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_RATE = 0.0005
FILL_TOLERANCE = 0.011
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "盘中止损-5%": {"stop": ("intra", 0.05)},
    "盘中止损-8%": {"stop": ("intra", 0.08)},
    "盘中止损-10%": {"stop": ("intra", 0.10)},
    "收盘破-8%次日开盘走": {"stop": ("close", 0.08)},
    "持有5日仍亏次日走": {"time": 5},
    "持有10日仍亏次日走": {"time": 10},
    "跌停收盘次日开盘走": {"ld": True},
    "不补亏损仓": {"noadd": True},
    "不补亏损仓+收盘破-8%": {"noadd": True, "stop": ("close", 0.08)},
}


# ---------------------------------------------------------------- fills & trips


@dataclass(eq=False)
class RoundTrip:
    code: str
    name: str
    fills: pd.DataFrame
    start: pd.Timestamp = field(init=False)
    end: pd.Timestamp = field(init=False)

    def __post_init__(self) -> None:
        # Bar lookups are midnight-indexed; keep intraday timestamps only for fill ordering.
        fills = self.fills.copy()
        fills["date"] = pd.to_datetime(fills["date"]).dt.normalize()
        self.fills = fills
        self.start = fills["date"].min()
        self.end = fills["date"].max()

    @property
    def n_buys(self) -> int:
        return int((self.fills["side"] == "buy").sum())


def load_fills(path: str | Path) -> pd.DataFrame:
    """Read a fills CSV: name,code,date,side,price,qty,amount,fee (code may be blank)."""
    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"code": str})
    need = {"name", "date", "side", "price", "qty", "amount", "fee"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"fills CSV missing columns: {sorted(missing)}")
    if "code" not in df.columns:
        df["code"] = ""
    df["code"] = df["code"].fillna("").astype(str).str.strip().str.upper()
    df["side"] = df["side"].astype(str).str.strip().str.lower()
    bad = sorted(set(df["side"]) - {"buy", "sell"})
    if bad:
        raise ValueError(f"side must be buy/sell, got {bad}")
    # Keep clock time so same-day sell-then-buy sorts correctly; RoundTrip normalizes later.
    df["date"] = pd.to_datetime(df["date"])
    for col in ("price", "qty", "amount", "fee"):
        df[col] = pd.to_numeric(df[col], errors="raise").astype(float)
    df["qty"] = df["qty"].abs()
    df["fee"] = df["fee"].fillna(0.0)
    return df


def build_round_trips(fills: pd.DataFrame) -> tuple[list[RoundTrip], list[str]]:
    """Split fills into round trips that start flat and return to zero shares."""
    trips: list[RoundTrip] = []
    open_positions: list[str] = []
    # Preserve transcript/CSV order within a day. Buy-before-sell reordering merges
    # overnight flatten+reopen (清仓再买 / 做T) when the CSV is date-only.
    order = fills.assign(_ord=np.arange(len(fills))).sort_values(["code", "date", "_ord"], kind="stable")
    for (code, name), group in order.groupby(["code", "name"], sort=False):
        pos, rows = 0.0, []
        for _, row in group.iterrows():
            rows.append(row)
            pos += row["qty"] if row["side"] == "buy" else -row["qty"]
            if abs(pos) < 1e-6:
                trips.append(RoundTrip(code, name, pd.DataFrame(rows).drop(columns="_ord")))
                rows = []
        if rows:
            open_positions.append(f"{name}({code}) 剩余 {pos:g} 股")
    return trips, open_positions


def trip_pnl(fills: pd.DataFrame) -> float:
    buys = fills[fills["side"] == "buy"]
    sells = fills[fills["side"] == "sell"]
    return float(sells["amount"].sum() - buys["amount"].sum() - fills["fee"].sum())


def max_exposure(fills: pd.DataFrame) -> float:
    shares, cost, peak = 0.0, 0.0, 0.0
    for _, row in fills.iterrows():
        if row["side"] == "buy":
            shares += row["qty"]
            cost += row["amount"]
        elif shares > 0:
            cost -= cost * row["qty"] / shares
            shares -= row["qty"]
        peak = max(peak, cost)
    return peak


# ---------------------------------------------------------------- market data


def limit_pct(code: str, name: str = "") -> float:
    from core.cn_boards import cn_board

    board = cn_board(code)
    if board == "bse":
        return 0.30
    if board in {"chinext", "star"}:
        return 0.20
    if "ST" in str(name or "").upper():
        return 0.05
    return 0.10


def prepare_bars(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize a TickFlow kline frame to d/open/high/low/close/pc sorted by date."""
    df = raw.copy()
    if "d" not in df.columns:
        ts = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df["d"] = ts.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    df = df.sort_values("d").reset_index(drop=True)
    df["pc"] = df["close"].shift(1)
    return df[["d", "open", "high", "low", "close", "pc"]]


def fetch_bars(codes: list[str], count: int = 500) -> dict[str, pd.DataFrame]:
    from integrations.tickflow_client import TickFlowClient

    client = TickFlowClient(api_key=os.environ.get("TICKFLOW_API_KEY", ""))
    out: dict[str, pd.DataFrame] = {}
    for code in codes:
        raw = client.get_klines(code, period="1d", count=count, adjust="none")
        if raw is not None and not raw.empty:
            out[code] = prepare_bars(raw)
    return out


def resolve_codes(fills: pd.DataFrame) -> pd.DataFrame:
    """Fill blank ``code`` cells by exact A-share name match from a TickFlow quote snapshot."""
    blank = fills["code"] == ""
    if not blank.any():
        return fills
    from integrations.tickflow_client import TickFlowClient

    client = TickFlowClient(api_key=os.environ.get("TICKFLOW_API_KEY", ""))
    quotes = client.get_quotes(universes=["CN_Equity_A"])
    name_to_code = {str((q.get("ext") or {}).get("name", "")).replace(" ", ""): sym for sym, q in quotes.items()}
    fills = fills.copy()
    fills.loc[blank, "code"] = fills.loc[blank, "name"].str.replace(" ", "").map(name_to_code).fillna("")
    return fills


def fills_inside_bars(trip: RoundTrip, bars: pd.DataFrame) -> bool:
    """False when any fill price is outside its day's raw range (wrong listing, adjusted bars, typo)."""
    by_day = bars.set_index("d")
    for _, row in trip.fills.iterrows():
        day = pd.Timestamp(row["date"]).normalize()
        if day not in by_day.index:
            return False
        bar = by_day.loc[day]
        if not (bar["low"] - FILL_TOLERANCE <= row["price"] <= bar["high"] + FILL_TOLERANCE):
            return False
    return True


# ---------------------------------------------------------------- per-trip features


def prior_features(trip: RoundTrip, bars: pd.DataFrame) -> dict[str, float]:
    pre = bars[bars["d"] < trip.start]
    if len(pre) < 21:
        return {"prior_limit_ups": np.nan, "prior_runup_pct": np.nan}
    lim = limit_pct(trip.code, trip.name)
    chg = pre["close"] / pre["pc"] - 1.0
    return {
        "prior_limit_ups": float((chg.tail(10) >= lim * 0.95).sum()),
        "prior_runup_pct": float((pre["close"].iloc[-1] / pre["close"].iloc[-21] - 1.0) * 100.0),
    }


def limit_down_closes_held(trip: RoundTrip, bars: pd.DataFrame) -> int:
    held = bars[(bars["d"] > trip.start) & (bars["d"] <= trip.end)]
    lim = limit_pct(trip.code, trip.name)
    return int((held["close"] <= (held["pc"] * (1 - lim)).round(2) + 0.001).sum())


def averaged_down(trip: RoundTrip) -> bool:
    shares, cost = 0.0, 0.0
    for _, row in trip.fills.iterrows():
        if row["side"] == "buy":
            if shares > 0 and row["price"] < cost / shares:
                return True
            shares += row["qty"]
            cost += row["amount"]
        elif shares > 0:
            cost -= cost * row["qty"] / shares
            shares -= row["qty"]
    return False


def index_shadow_pnl(trip: RoundTrip, closes: pd.Series) -> float:
    """Replay the trip's exact cash flows on an index (daily close, no index costs)."""
    units, cash, pos = 0.0, 0.0, 0.0
    for _, row in trip.fills.iterrows():
        px = float(closes[closes.index <= row["date"]].iloc[-1])
        if row["side"] == "buy":
            units += row["amount"] / px
            cash -= row["amount"]
            pos += row["qty"]
        else:
            frac = row["qty"] / pos if pos else 1.0
            cash += units * frac * px
            units -= units * frac
            pos -= row["qty"]
    return cash


# ---------------------------------------------------------------- exit-rule replay


def _sell_fee(amount: float) -> float:
    return max(COMMISSION_MIN, amount * COMMISSION_RATE) + amount * STAMP_RATE


def _buy_fee(amount: float) -> float:
    return max(COMMISSION_MIN, amount * COMMISSION_RATE)


def _rule_exit_price(rule: dict[str, Any], bar: pd.Series, prev: pd.Series, avg: float, held_days: int, lim: float):
    """Price at which ``rule`` exits today, or None. A one-price limit-down day cannot be sold."""
    limit_down_close = prev["close"] <= round(prev["pc"] * (1 - lim), 2) + 0.001 if pd.notna(prev["pc"]) else False
    today_locked = bar["high"] == bar["low"] and pd.notna(bar["pc"]) and bar["close"] <= bar["pc"] * (1 - lim) + 0.011
    if today_locked:
        return None
    kind, pct = rule.get("stop", (None, None))
    if kind == "intra":
        stop = avg * (1 - pct)
        if bar["open"] <= stop:
            return float(bar["open"])
        if bar["low"] <= stop:
            return float(stop)
    if kind == "close" and prev["close"] <= avg * (1 - pct):
        return float(bar["open"])
    if "time" in rule and held_days >= rule["time"] and prev["close"] < avg:
        return float(bar["open"])
    if rule.get("ld") and limit_down_close:
        return float(bar["open"])
    return None


@dataclass
class _Book:
    shares: float = 0.0
    cost: float = 0.0
    cash: float = 0.0
    actual_pos: float = 0.0

    def avg(self) -> float:
        return self.cost / self.shares if self.shares else 0.0


def _apply_fill(book: _Book, row: pd.Series, rule: dict[str, Any] | None) -> None:
    if row["side"] == "buy":
        skip = bool(
            rule
            and rule.get("noadd")
            and ((book.shares > 0 and row["price"] < book.avg()) or (book.shares == 0 and book.actual_pos > 0))
        )
        book.actual_pos += row["qty"]
        if skip:
            return
        fee = row["fee"] if rule is None else _buy_fee(row["amount"])
        book.shares += row["qty"]
        book.cost += row["amount"] + fee
        book.cash -= row["amount"] + fee
        return
    frac = row["qty"] / book.actual_pos if book.actual_pos else 1.0
    qty = book.shares if frac >= 1 - 1e-9 else round(book.shares * frac)
    book.actual_pos -= row["qty"]
    if qty <= 0:
        return
    amount = row["price"] * qty
    book.cash += amount - (row["fee"] if rule is None else _sell_fee(amount))
    book.cost -= book.cost * qty / book.shares
    book.shares -= qty


def replay_trip(trip: RoundTrip, bars: pd.DataFrame, rule: dict[str, Any] | None) -> float:
    """Net P&L of ``trip`` if ``rule`` had been followed (None = actual fills, actual fees)."""
    lim = limit_pct(trip.code, trip.name)
    by_day = bars.set_index("d")
    days = sorted(
        set(by_day.index[(by_day.index >= trip.start) & (by_day.index <= trip.end)]) | set(trip.fills["date"])
    )
    book, held_days, prev = _Book(), 0, None
    for day in days:
        bar = by_day.loc[day] if day in by_day.index else None
        if rule and book.shares > 0 and bar is not None and prev is not None:
            price = _rule_exit_price(rule, bar, prev, book.avg(), held_days, lim)
            if price is not None:
                amount = price * book.shares
                return book.cash + amount - _sell_fee(amount)
        for _, row in trip.fills[trip.fills["date"] == day].iterrows():
            _apply_fill(book, row, rule)
        held_days += int(book.shares > 0)
        prev = bar if bar is not None else prev
    return book.cash


def rule_table(trips: list[RoundTrip], bars: dict[str, pd.DataFrame], rules: dict[str, dict]) -> pd.DataFrame:
    """Per-rule delta vs actual, with leave-top-2-out and chronological-half robustness checks."""
    base = np.array([replay_trip(t, bars[t.code], None) for t in trips])
    order = np.argsort([t.start for t in trips], kind="stable")
    first_half = np.zeros(len(trips), dtype=bool)
    first_half[order[: len(trips) // 2]] = True
    rows = []
    for name, rule in rules.items():
        delta = np.array([replay_trip(t, bars[t.code], rule) for t in trips]) - base
        top2 = np.sort(delta)[-2:].sum() if len(delta) >= 2 else delta.sum()
        rows.append(
            {
                "规则": name,
                "相对实际": round(delta.sum()),
                "变好笔数": int((delta > 1).sum()),
                "变差笔数": int((delta < -1).sum()),
                "去掉最大2笔后": round(delta.sum() - top2),
                "前半样本": round(delta[first_half].sum()),
                "后半样本": round(delta[~first_half].sum()),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- lot solver


def solve_lots(buy_amount: float, pnl: float, total_fee: float, entry_bar: pd.Series, exit_bar: pd.Series):
    """Recover (qty, buy_price, sell_price) for a single-buy/single-sell trip from summary numbers.

    Returns the candidate when exactly one 100-share lot size fits both days' raw ranges, else None.
    """
    proceeds = buy_amount + pnl + total_fee
    found = []
    for qty in range(100, 1_000_001, 100):
        buy_px = buy_amount / qty
        if buy_px < entry_bar["low"] - 0.005:
            break
        if buy_px > entry_bar["high"] + 0.005 or abs(round(buy_px, 3) * qty - buy_amount) > 0.02:
            continue
        sell_px = proceeds / qty
        if exit_bar["low"] - 0.02 <= sell_px <= exit_bar["high"] + 0.02:
            found.append((qty, round(buy_px, 3), round(sell_px, 3)))
    exact = [c for c in found if abs(round(c[1], 2) - c[1]) < 1e-9]
    if len(found) == 1:
        return found[0]
    return exact[0] if len(exact) == 1 else None


# ---------------------------------------------------------------- summaries


def summarize(table: pd.DataFrame) -> dict[str, float]:
    wins = table[table["pnl"] > 0]
    losses = table[table["pnl"] <= 0]
    win_rate = len(wins) / len(table) if len(table) else 0.0
    avg_win = float(wins["pnl"].mean()) if len(wins) else 0.0
    avg_loss = float(-losses["pnl"].mean()) if len(losses) else 0.0
    loss_total = float(-losses["pnl"].sum())
    top2_losses = float(-losses["pnl"].nsmallest(2).sum())
    return {
        "trips": len(table),
        "total_pnl": round(float(table["pnl"].sum()), 2),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy_per_trip": round(win_rate * avg_win - (1 - win_rate) * avg_loss, 2),
        "breakeven_win_rate": round(avg_loss / (avg_win + avg_loss), 4) if avg_win + avg_loss else None,
        "fees": round(float(table["fees"].sum()), 2),
        "top10_loss_share": round(float(-losses["pnl"].nsmallest(10).sum()) / loss_total, 4) if loss_total else 0,
        "total_without_top2_losses": round(float(table["pnl"].sum()) + top2_losses, 2),
    }


def group_table(table: pd.DataFrame, column: str, bins: list[float], labels: list[str]) -> pd.DataFrame:
    cut = pd.cut(table[column], bins=bins, labels=labels)
    return (
        table.groupby(cut, observed=True)
        .agg(笔数=("pnl", "size"), 合计=("pnl", "sum"), 胜率=("pnl", lambda s: round((s > 0).mean() * 100, 1)))
        .round(0)
    )


def trip_table(trips: list[RoundTrip], bars: dict[str, pd.DataFrame], indices: dict[str, pd.Series]) -> pd.DataFrame:
    rows = []
    for t in trips:
        b = bars[t.code]
        row = {
            "name": t.name,
            "code": t.code,
            "start": t.start,
            "end": t.end,
            "holding_days": (t.end - t.start).days,
            "n_buys": t.n_buys,
            "pnl": trip_pnl(t.fills),
            "fees": float(t.fills["fee"].sum()),
            "max_exposure": max_exposure(t.fills),
            "averaged_down": averaged_down(t),
            "limit_down_closes": limit_down_closes_held(t, b),
            **prior_features(t, b),
        }
        for code, closes in indices.items():
            row[f"index_{code}"] = index_shadow_pnl(t, closes)
        rows.append(row)
    return pd.DataFrame(rows)


def _md(df: pd.DataFrame, index: bool = True) -> str:
    """Render a small DataFrame as a GitHub Markdown table without optional dependencies."""
    frame = df.reset_index() if index else df

    def cell(v: Any) -> str:
        if isinstance(v, float) and v.is_integer():
            return f"{int(v):,}"
        if isinstance(v, float):
            return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.4g}"
        return str(v)

    lines = ["| " + " | ".join(map(str, frame.columns)) + " |", "|" + "---|" * len(frame.columns)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in frame.itertuples(index=False)]
    return "\n".join(lines)


def write_report(out_dir: Path, table: pd.DataFrame, rules: pd.DataFrame, notes: list[str]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    s = summarize(table)
    table.assign(month=table["end"].dt.strftime("%Y-%m")).to_csv(out_dir / "trips.csv", index=False)
    rules.to_csv(out_dir / "exit_rules.csv", index=False)
    sections = [
        "# 交易复盘报告",
        "## 期望值\n" + _md(pd.Series(s).rename_axis("指标").to_frame("值")),
        "## 按清仓月份\n"
        + _md(table.groupby(table["end"].dt.strftime("%Y-%m"))["pnl"].agg(笔数="size", 合计="sum").round(0)),
        "## 同时点换成指数\n" + _index_section(table),
        "## 按持有自然日\n"
        + _md(
            group_table(table, "holding_days", [-1, 1, 3, 7, 14, 30, 1e9], ["≤1", "2-3", "4-7", "8-14", "15-30", ">30"])
        ),
        "## 按买入前10日涨停数\n" + _md(group_table(table, "prior_limit_ups", [-1, 0, 1, 99], ["0", "1", "≥2"])),
        "## 按买入前20日涨幅\n"
        + _md(group_table(table, "prior_runup_pct", [-1e9, 0, 15, 30, 1e9], ["下跌", "0~15%", "15~30%", ">30%"])),
        "## 补仓与跌停\n" + _risk_section(table),
        "## 离场规则回放（事后挑选，仅作线索）\n" + _md(rules, index=False),
        "## 数据口径\n" + "\n".join(f"- {n}" for n in notes),
    ]
    path = out_dir / "report.md"
    path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _index_section(table: pd.DataFrame) -> str:
    cols = [c for c in table.columns if c.startswith("index_")]
    lines = [f"- 实际合计 {table['pnl'].sum():+,.0f}"]
    lines += [f"- 同时点 {c.removeprefix('index_')} 合计 {table[c].sum():+,.0f}" for c in cols]
    return "\n".join(lines)


def _risk_section(table: pd.DataFrame) -> str:
    ad = table[table["averaged_down"]]
    ld = table[table["limit_down_closes"] > 0]
    return "\n".join(
        [
            f"- 有过低于成本补仓的: {len(ad)} 笔，合计 {ad['pnl'].sum():+,.0f}",
            f"- 持有期间吃过跌停收盘的: {len(ld)} 笔，合计 {ld['pnl'].sum():+,.0f}",
            f"- 最高占用资金 中位 {table['max_exposure'].median():,.0f} / 90分位 {table['max_exposure'].quantile(0.9):,.0f}"
            f" / 最大 {table['max_exposure'].max():,.0f}",
        ]
    )


# ---------------------------------------------------------------- CLI


def _guard_out_dir(out_dir: Path, allow_repo: bool) -> None:
    resolved = out_dir.expanduser().resolve()
    if not allow_repo and (resolved == REPO_ROOT or REPO_ROOT in resolved.parents):
        raise SystemExit(f"拒绝把个人交易数据写进仓库目录: {resolved}（换一个仓库外的 --out-dir）")


def cmd_analyze(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    _guard_out_dir(out_dir, args.allow_repo_output)
    fills = resolve_codes(load_fills(args.fills))
    unresolved = sorted(set(fills.loc[fills["code"] == "", "name"]))
    trips, open_positions = build_round_trips(fills[fills["code"] != ""])
    if args.since:
        trips = [t for t in trips if t.end >= pd.Timestamp(args.since)]
    bars = fetch_bars(sorted({t.code for t in trips}))
    usable = [t for t in trips if t.code in bars and fills_inside_bars(t, bars[t.code])]
    skipped = sorted({f"{t.name}({t.code})" for t in trips if t not in usable})
    indices = {code: b.set_index("d")["close"] for code, b in fetch_bars(list(INDEX_CODES)).items()}
    table = trip_table(usable, bars, indices)
    rules = rule_table(usable, bars, DEFAULT_RULES)
    notes = [
        f"纳入 {len(usable)} 笔已清仓交易；日线为不复权价（adjust=none），分红/送转未计入",
        f"未识别代码: {', '.join(unresolved) or '无'}",
        f"成交价落在当日价格区间外而剔除（港股同名、数据错误等）: {', '.join(skipped) or '无'}",
        f"未清仓: {'; '.join(open_positions) or '无'}",
        "离场规则回放按日线：盘中止损按止损价成交偏乐观；一字跌停当天不可卖；T+1 已遵守",
        "规则与分组都是事后挑选，变好须同时通过「去掉最大2笔」和前后半样本同号，否则只算线索",
    ]
    print(write_report(out_dir, table, rules, notes))


def cmd_solve_lots(args: argparse.Namespace) -> None:
    _guard_out_dir(Path(args.out).parent, args.allow_repo_output)
    summary = pd.read_csv(args.summary, encoding="utf-8-sig", dtype={"code": str})
    summary["entry_date"] = pd.to_datetime(summary["entry_date"]).dt.normalize()
    summary["exit_date"] = pd.to_datetime(summary["exit_date"]).dt.normalize()
    bars = fetch_bars(sorted(set(summary["code"])))
    rows, unsolved = [], []
    for _, r in summary.iterrows():
        b = bars.get(r["code"])
        found = None
        if b is not None:
            by_day = b.set_index("d")
            if r["entry_date"] in by_day.index and r["exit_date"] in by_day.index:
                found = solve_lots(
                    r["buy_amount"], r["pnl"], r["fees"], by_day.loc[r["entry_date"]], by_day.loc[r["exit_date"]]
                )
        if found is None:
            unsolved.append(f"{r['name']} {r['entry_date'].date()}")
            continue
        qty, buy_px, sell_px = found
        buy_fee = max(COMMISSION_MIN, r["buy_amount"] * COMMISSION_RATE)
        base = {"name": r["name"], "code": r["code"], "qty": qty}
        rows.append(
            {
                **base,
                "date": r["entry_date"].date(),
                "side": "buy",
                "price": buy_px,
                "amount": r["buy_amount"],
                "fee": round(buy_fee, 2),
            }
        )
        rows.append(
            {
                **base,
                "date": r["exit_date"].date(),
                "side": "sell",
                "price": sell_px,
                "amount": round(sell_px * qty, 2),
                "fee": round(r["fees"] - buy_fee, 2),
            }
        )
    pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"solved {len(rows) // 2}, unsolved {len(unsolved)}: {', '.join(unsolved) or '-'}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Personal A-share trade review (fills -> attribution -> replay)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="Attribute realized P&L and replay exit rules")
    a.add_argument("--fills", required=True)
    a.add_argument("--out-dir", required=True)
    a.add_argument("--since", default=None, help="Only round trips closed on/after this date (YYYY-MM-DD)")
    a.add_argument("--allow-repo-output", action="store_true")
    a.set_defaults(func=cmd_analyze)
    s = sub.add_parser("solve-lots", help="Recover fills for single-buy/single-sell summary rows")
    s.add_argument("--summary", required=True, help="CSV: name,code,entry_date,exit_date,buy_amount,pnl,fees")
    s.add_argument("--out", required=True)
    s.add_argument("--allow-repo-output", action="store_true")
    s.set_defaults(func=cmd_solve_lots)
    args = parser.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    args.func(args)


if __name__ == "__main__":
    main()
