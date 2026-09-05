"""跑风格择时体检:池子自己的近期强弱能不能预测池子接下来的表现。

结论与全部对照见 :mod:`core.style_timing_eval` 的模块 docstring。一句话:
回看 5 日在**收益口径**上活下来(首轮实测 T+5 强-弱 +4.58pct、相位 t=+5.40、
月内置换 p=0.027,区间 2026-05-25~2026-08-28),但**胜率口径没过月内置换**
(p=0.281)——而胜率是第一优先级。回看 20 日是「避开 2026-07」的伪装,
水温标签换环移对照后没有方向性。而且最好的情况也只是少亏:强档 T+5 仍是 -0.53%。

**数会每月变,别把上面这几个当常量。**引用时读产物 JSON 里的 ``eval_window``。

用法::

    python scripts/evaluate_style_timing.py --horizon 5
    python scripts/evaluate_style_timing.py --horizon 10 --value win
    python scripts/evaluate_style_timing.py --no-notify   # 只出报告不推送

**这是测量,不改任何生产开关。**69 天 / 4 个可用月不足以动闸门;每月重跑一次,
看结论能否跨周期稳定,是它存在的唯一理由。

**跑一轮约 40 分钟**:池子 1972 只 + 市场对照 400 只,逐只取价,约 1 只/秒。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import _bootstrap  # noqa: F401

from core.style_timing_eval import (
    ROUND_TRIP_COST_PCT,
    TRAIL_WINDOWS,
    DayRow,
    evaluate_style_timing,
    report_to_dict,
)

# docs/evidence 而非 artifacts/——后者在 .gitignore 里,CI 的 upload-artifact
# 按路径匹配不到文件,证据会静默丢失。
EVIDENCE_DIR = Path("docs/evidence")
# 市场对照的取样数。全市场逐只拉价太慢,固定种子抽样即可——它只用来否定。
MARKET_SAMPLE = 400
MARKET_SEED = 7
# 回看最长 20 日 + 前瞻最长 15 日,两头都要留够,否则边界日会被静默丢掉。
LOOKBACK_PAD_DAYS = 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="池子近期强弱的择时体检")
    parser.add_argument("--horizon", type=int, default=5, help="前瞻交易日数")
    parser.add_argument(
        "--value",
        default="forward",
        choices=["forward", "win"],
        help="forward=前瞻收益均值, win=先触碰胜率",
    )
    parser.add_argument("--start", default="", help="起始日期,默认取 signal_outcomes 最早一天")
    parser.add_argument("--up", type=float, default=5.0, help="先触碰口径的止盈幅度")
    parser.add_argument("--down", type=float, default=5.0, help="先触碰口径的止损幅度")
    parser.add_argument("--max-hold", type=int, default=15, help="先触碰口径的最长持有交易日")
    parser.add_argument("--no-notify", action="store_true", help="不推送飞书")
    parser.add_argument("--out", default=str(EVIDENCE_DIR), help="报告输出目录")
    return parser.parse_args()


def _d8(value: object) -> str:
    """把 20260901 与 2026-09-01 统一成后者。"""
    s = str(value or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def _sfx(code: object) -> str:
    """补后缀。zfill(6) 是必需的:部分表把代码存成数字,000100 会读回成 100。"""
    cd = str(code or "").split(".")[0].strip().zfill(6)
    if cd[:1] == "6":
        return f"{cd}.SH"
    return f"{cd}.SZ" if cd[:1] in "03" else f"{cd}.BJ"


def load_pool_by_day(start: str) -> dict[str, list[str]]:
    """signal_outcomes 逐日的池子成分。"""
    from integrations.supabase_base import create_admin_client, is_admin_configured

    if not is_admin_configured():
        raise SystemExit("需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    client = create_admin_client()
    rows: list[dict] = []
    offset, page = 0, 1000
    while True:
        # 单页静默截断在 1000 行,必须翻页。
        chunk = (
            client.table("signal_outcomes")
            .select("trade_date,code")
            .order("trade_date")
            .range(offset, offset + page - 1)
            .execute()
            .data
            or []
        )
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    pool: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        trade_date, code = _d8(row.get("trade_date")), row.get("code")
        if trade_date and code and (not start or trade_date >= start):
            pool[trade_date].add(_sfx(code))
    return {d: sorted(v) for d, v in pool.items() if v}


def load_closes(symbols: list[str], start: str, end: str) -> dict[str, list[tuple[str, float]]]:
    """逐只拉收盘,按日期升序。取不到的票直接跳过,不插补。"""
    from integrations.stock_hist_repository import get_stock_hist

    series: dict[str, list[tuple[str, float]]] = {}
    for i, symbol in enumerate(symbols, 1):
        try:
            frame = get_stock_hist(symbol, start, end)
        except Exception as exc:  # 单票失败不该让整轮体检停下
            print(f"  [skip] {symbol}: {exc}")
            continue
        if frame is None or frame.empty or "收盘" not in frame.columns:
            continue
        date_col = "日期" if "日期" in frame.columns else frame.columns[0]
        items: list[tuple[str, float]] = []
        for _, row in frame.iterrows():
            close = row.get("收盘")
            try:
                value = float(close)
            except (TypeError, ValueError):
                continue
            if value > 0:
                items.append((str(row.get(date_col))[:10], value))
        if items:
            series[symbol] = sorted(items)
        if i % 100 == 0:
            print(f"  取价进度 {i}/{len(symbols)}")
    return series


class Prices:
    """收盘序列 + 日期到下标的映射。回看与前瞻都按交易日下标走,不按日历天。"""

    def __init__(self, series: dict[str, list[tuple[str, float]]]) -> None:
        self.series = series
        self.posn = {sym: {d: i for i, (d, _) in enumerate(items)} for sym, items in series.items()}

    def trail(self, symbol: str, day: str, window: int) -> float | None:
        """当日回看 window 个交易日的已实现涨跌。不含未来信息。"""
        items = self.series.get(symbol)
        idx = self.posn.get(symbol, {}).get(day)
        if not items or idx is None or idx - window < 0:
            return None
        prev, now = items[idx - window][1], items[idx][1]
        return (now - prev) / prev * 100.0 if prev > 0 else None

    def forward(self, symbol: str, day: str, horizon: int) -> float | None:
        """当日进、持有 horizon 个交易日的收益,已扣双边成本。"""
        items = self.series.get(symbol)
        idx = self.posn.get(symbol, {}).get(day)
        if not items or idx is None or idx + horizon >= len(items):
            return None
        entry = items[idx][1]
        if entry <= 0:
            return None
        return (items[idx + horizon][1] - entry) / entry * 100.0 - ROUND_TRIP_COST_PCT

    def first_touch(self, symbol: str, day: str, up: float, down: float, max_hold: int) -> float | None:
        """先触碰口径:先摸到 +up 记赢,先摸到 -down 记亏,都没摸到按末日正负判。

        用收盘判,不是盘中最高最低——手上只有收盘价,拿 high/low 会高估触发率。
        """
        items = self.series.get(symbol)
        idx = self.posn.get(symbol, {}).get(day)
        if not items or idx is None or idx + 1 >= len(items) or items[idx][1] <= 0:
            return None
        entry = items[idx][1]
        end = min(idx + max_hold, len(items) - 1)
        if end <= idx:
            return None
        for j in range(idx + 1, end + 1):
            ret = (items[j][1] - entry) / entry * 100.0 - ROUND_TRIP_COST_PCT
            if ret >= up:
                return 1.0
            if ret <= -down:
                return 0.0
        return 1.0 if (items[end][1] - entry) / entry * 100.0 - ROUND_TRIP_COST_PCT > 0 else 0.0


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_rows(
    pool: dict[str, list[str]],
    prices: Prices,
    market: list[str],
    args: argparse.Namespace,
) -> list[DayRow]:
    """逐日聚合。池子成分每天在换,所以逐只算完再取当日均值,不能拿一条池子指数。"""
    rows: list[DayRow] = []
    for day in sorted(pool):
        members = pool[day]
        pool_trail: dict[int, float | None] = {}
        market_trail: dict[int, float | None] = {}
        for window in TRAIL_WINDOWS:
            pool_trail[window] = _mean([v for v in (prices.trail(s, day, window) for s in members) if v is not None])
            market_trail[window] = _mean([v for v in (prices.trail(s, day, window) for s in market) if v is not None])
        fwd = [v for v in (prices.forward(s, day, args.horizon) for s in members) if v is not None]
        win = [
            v for v in (prices.first_touch(s, day, args.up, args.down, args.max_hold) for s in members) if v is not None
        ]
        mkt_fwd = [v for v in (prices.forward(s, day, args.horizon) for s in market) if v is not None]
        mkt_win = [
            v for v in (prices.first_touch(s, day, args.up, args.down, args.max_hold) for s in market) if v is not None
        ]
        rows.append(
            DayRow(
                date=day,
                pool_trail=pool_trail,
                pool_forward=_mean(fwd),
                pool_win=(_mean(win) * 100.0) if win else None,
                n_pool=len(fwd) if args.value == "forward" else len(win),
                market_trail=market_trail,
                market_forward=_mean(mkt_fwd),
                market_win=(_mean(mkt_win) * 100.0) if mkt_win else None,
                n_market=len(mkt_fwd) if args.value == "forward" else len(mkt_win),
            )
        )
    return rows


def render_markdown(payload: dict) -> str:
    kind = "前瞻收益" if payload["value_kind"] == "forward" else "先触碰胜率"
    unit = "%" if payload["value_kind"] == "forward" else "%"
    window = payload.get("eval_window") or ["-", "-"]
    lines = [
        f"## 风格择时体检 T+{payload['horizon']} · {kind}",
        "",
        f"- 评估区间 **{window[0]} ~ {window[-1]}**,{payload['days']} 天 / "
        f"{len(payload['months'])} 个月,不重叠步长 {payload.get('stride')} 天",
        f"- 基准(任意一天进池子): **{payload['baseline']:+.2f}{unit}**"
        if payload.get("baseline") is not None
        else "- 基准: 无",
        "",
        "| 回看 | 弱档 | 强档 | 强-弱 | 相位 t | 留一月同号 | 月内独立 | 置换 p | 市场·信号侧 | 市场·前瞻侧 | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, block in payload["windows"].items():
        full = block["full_sample"]
        phase = block["phase_scan"]
        lomo = block["leave_one_month_out"]
        within = block["within_month"]
        perm = block["month_block_permutation"]
        mkt_sig = block["market_signal_control"]
        mkt_fwd = block["market_forward_control"]

        def fmt(value: float | None, sign: bool = False) -> str:
            if value is None:
                return "-"
            return f"{value:+.2f}" if sign else f"{value:.2f}"

        lines.append(
            f"| {name.replace('trail', '')} 日 | {fmt(full['weak'], True)} | "
            f"{fmt(full['strong'], True)} | **{fmt(full['spread'], True)}** | "
            f"{fmt(phase.get('t'), True)} ({phase.get('positive') or '-'}) | "
            f"{lomo.get('same_sign') or '-'} | {within.get('positive') or '-'} | "
            f"{fmt(perm.get('p_value'))} | {fmt(mkt_sig['spread'], True)} | "
            f"{fmt(mkt_fwd['spread'], True)} | {block['verdict']} |"
        )
    for note in payload.get("notes") or []:
        lines += ["", f"> {note}"]
    return "\n".join(lines)


def _notify(markdown: str, payload: dict) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[style] 未配置 FEISHU_WEBHOOK_URL,跳过推送")
        return
    from utils.feishu import send_feishu_notification

    window = payload.get("eval_window") or ["-"]
    title = f"风格择时体检 T+{payload['horizon']}|{window[-1]}"
    ok = send_feishu_notification(webhook, title, markdown)
    print("[style] feishu sent" if ok else "[style] feishu failed")


def load_market_control(first_day: str, last_day: str) -> list[str]:
    """市场对照池:第一天就已上市、且到最后一天仍未摘牌的票。

    用 PIT 名单而不是当前存续名单——后者带生存偏差,会让对照组系统性偏强。
    """
    from integrations.pit_universe import fetch_pit_symbols, tradable_on

    symbols = fetch_pit_symbols()
    head = {s.code for s in tradable_on(symbols, first_day.replace("-", ""))}
    tail = {s.code for s in tradable_on(symbols, last_day.replace("-", ""))}
    return sorted(_sfx(code) for code in head & tail)


def main() -> int:
    # 逐只取价要跑十几分钟,stdout 非 tty 时默认整块缓冲,CI 日志和后台重定向都会
    # 一直空着——看不出是在跑还是卡死了。改行缓冲,进度才是实时的。
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    pool = load_pool_by_day(args.start)
    if not pool:
        raise SystemExit("signal_outcomes 无可用数据")
    days = sorted(pool)
    print(f"池子 {len(days)} 天 | {days[0]} ~ {days[-1]}")

    # 两头都要留够:回看最长 20 日、前瞻最长 max-hold,边界日否则被静默丢掉。
    start = (date.fromisoformat(days[0]) - timedelta(days=LOOKBACK_PAD_DAYS)).isoformat()
    end = (date.fromisoformat(days[-1]) + timedelta(days=LOOKBACK_PAD_DAYS)).isoformat()

    members = sorted({s for ds in pool.values() for s in ds})
    print(f"池子成分 {len(members)} 只,取价 {start} ~ {end}")
    series = load_closes(members, start, end)

    try:
        held = set(members)
        universe = [s for s in load_market_control(days[0], days[-1]) if s not in held]
    except Exception as exc:
        print(f"[market] 全市场清单取数失败,跳过市场对照: {exc}")
        universe = []
    market = random.Random(MARKET_SEED).sample(universe, min(MARKET_SAMPLE, len(universe)))
    if market:
        print(f"市场对照 {len(market)} 只")
        series.update(load_closes(market, start, end))
    market = [s for s in market if s in series]

    prices = Prices(series)
    rows = build_rows(pool, prices, market, args)
    # 胜率口径的窗口是 max_hold(先触碰最长能拖到那天),不是 horizon。
    stride = args.horizon if args.value == "forward" else args.max_hold
    report = evaluate_style_timing(rows, horizon=args.horizon, value_kind=args.value, stride=stride)
    payload = report_to_dict(report)
    payload["params"] = {
        "up": args.up,
        "down": args.down,
        "max_hold": args.max_hold,
        "cost_pct": ROUND_TRIP_COST_PCT,
        "market_sample": len(market),
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 胜率口径不依赖 horizon(它走 first_touch 的 up/down/max_hold),文件名不带 h,
    # 否则 --horizon 5 与 10 会写出两个内容相同的文件,后来的人会以为是两轮证据。
    name = f"style_timing_h{args.horizon}_forward.json" if args.value == "forward" else "style_timing_win.json"
    (out_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(payload)
    print(markdown)
    if not args.no_notify:
        _notify(markdown, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
