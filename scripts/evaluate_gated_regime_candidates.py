"""检验被水温闸门封死的候选：那些票放出来到底赚不赚钱。

回答的问题很具体：`NO_NEW_BUY_REGIMES`（RISK_OFF/CRASH/UNKNOWN/BLACK_SWAN）把
`allow_ai_review` 置 False，整层短路（workflows/daily_job_step2.py 的
`step3_symbols_info=[]`）。这些天的候选从未被送审、从未下单，所以生产上没有任何
收益记录可查——只能拿已落库的 `signal_observations` 反过来做反事实检验。

与既有脚本的分工：
  - scripts/evaluate_regime_forward.py 检验**指数**在各水温后的走势，不涉及候选票
  - scripts/evaluate_pattern_forward.py 按形态口径**重新筛票**，与漏斗实际产出无关
  - 本脚本读**漏斗当日真实产出的候选名单**，是唯一能回答「闸门封掉的是什么」的口径

口径与 core/pattern_forward_eval 完全一致，故结论可与 BEAR_REBOUND 豁免注释
（core/market_trade_mode.py 的 `_explicitly_allowed_regimes`）直接对比：
买点 T+1 开盘（漏斗收盘后出信号，实盘最早次日开盘），卖点 T+1+horizon 收盘，
扣 ROUND_TRIP_COST_PCT=0.202%，基准为同日同流动性门槛下的全市场等权，
按交易日等权汇总（命中多的日子不主导均值）。

用法::

    python scripts/evaluate_gated_regime_candidates.py --cands /tmp/gate_cands.json --cache /tmp/ic2y.csv
    python scripts/evaluate_gated_regime_candidates.py --status all --horizons 5,10
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from core.pattern_forward_eval import ROUND_TRIP_COST_PCT, summarize_horizon

GATED_REGIMES = ("RISK_OFF", "CRASH", "UNKNOWN", "BLACK_SWAN")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="被闸门封死的候选的前瞻收益检验")
    parser.add_argument("--cands", required=True, help="候选 JSON：{date: {regime, formal_l4[], all[]}}")
    parser.add_argument("--cache", required=True, help="行情缓存 CSV（ts_code,trade_date,open,close,amount）")
    parser.add_argument("--status", default="formal_l4", choices=["formal_l4", "all"], help="用哪一层候选")
    parser.add_argument("--horizons", default="5,10", help="持有期，逗号分隔")
    parser.add_argument("--min-amount-wan", type=float, default=8000.0, help="20 日均额下限（万元）")
    parser.add_argument("--json-out", default="", help="结构化结果输出路径")
    return parser.parse_args()


def load_market(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path, usecols=["ts_code", "trade_date", "open", "close", "amount"])
    frame["code"] = frame.ts_code.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
    frame["ds"] = pd.to_datetime(frame.trade_date.astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
    for col in ("open", "close", "amount"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.dropna(subset=["open", "close"]).sort_values(["code", "ds"])


def build_panels(frame: pd.DataFrame, min_amount_wan: float) -> tuple[dict, dict, list[str]]:
    """返回 (open_by_day, close_by_day, dates)，并按 20 日均额过滤流动性。"""
    frame = frame.copy()
    # amount 单位为千元（Tushare 口径），换成万元后与生产门槛对齐
    frame["amt_wan"] = frame.amount / 10.0
    frame["avg20"] = frame.groupby("code", sort=False).amt_wan.transform(
        lambda s: s.rolling(20, min_periods=10).mean().shift(1)
    )
    liquid = frame[frame.avg20 >= min_amount_wan]
    dates = sorted(frame.ds.unique())
    opens = {ds: dict(zip(g.code, g.open, strict=True)) for ds, g in frame.groupby("ds", sort=False)}
    closes = {ds: dict(zip(g.code, g.close, strict=True)) for ds, g in frame.groupby("ds", sort=False)}
    liquid_by_day = {ds: set(g.code) for ds, g in liquid.groupby("ds", sort=False)}
    return {"open": opens, "close": closes, "liquid": liquid_by_day}, {}, dates


def forward_return(panels: dict, dates: list[str], signal_ds: str, codes: set[str], horizon: int):
    """T+1 开盘买、T+1+horizon 收盘卖。返回 (net_pct, market_pct, hits)。"""
    if signal_ds not in dates:
        return None
    idx = dates.index(signal_ds)
    buy_i, sell_i = idx + 1, idx + 1 + horizon
    if sell_i >= len(dates):
        return None
    buy_ds, sell_ds = dates[buy_i], dates[sell_i]
    opens, closes = panels["open"].get(buy_ds, {}), panels["close"].get(sell_ds, {})
    universe = panels["liquid"].get(signal_ds, set())

    def _rets(pool):
        out = []
        for code in pool:
            o, c = opens.get(code), closes.get(code)
            if o and c and o > 0:
                out.append(100.0 * (c / o - 1.0))
        return out

    hit_rets = _rets(codes & universe)
    mkt_rets = _rets(universe)
    if not hit_rets or not mkt_rets:
        return None
    net = sum(hit_rets) / len(hit_rets) - ROUND_TRIP_COST_PCT
    market = sum(mkt_rets) / len(mkt_rets)
    return net, market, len(hit_rets)


def evaluate(cands: dict, panels: dict, dates: list[str], status: str, horizons: list[int]) -> dict:
    groups = {"GATED_ALL": GATED_REGIMES, "RISK_OFF": ("RISK_OFF",), "CRASH": ("CRASH",)}
    for regime in sorted({v["regime"] for v in cands.values()}):
        groups.setdefault(regime, (regime,))
    out = {}
    for label, regimes in groups.items():
        days = [d for d, v in cands.items() if v["regime"] in regimes]
        rows_by_h: dict[int, list[dict]] = {h: [] for h in horizons}
        for ds in sorted(days):
            codes = set(cands[ds][status])
            for h in horizons:
                got = forward_return(panels, dates, ds, codes, h)
                if got is not None:
                    net, market, hits = got
                    rows_by_h[h].append({"net": net, "market": market, "hits": hits})
        out[label] = {
            "candidate_days": len(days),
            "horizons": {h: summarize_horizon(h, rows_by_h[h]).as_dict() for h in horizons},
        }
    return out


def render(result: dict, status: str, horizons: list[int]) -> str:
    lines = [
        f"# 闸门封死的候选：前瞻收益检验（status={status}）",
        "",
        f"买点 T+1 开盘 / 卖点 T+1+H 收盘 / 扣往返成本 {ROUND_TRIP_COST_PCT}% / 基准=同日全市场等权",
        "",
        "| 分组 | H | 天数 | 日均命中 | 净收益 | 市场 | 净超额 | 为正日 | 判定 | 过成本 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for label, block in result.items():
        for h in horizons:
            r = block["horizons"][h]
            excess = "—" if r["net_excess_pct"] is None else f"{r['net_excess_pct']:+.2f}"
            net = "—" if r["net_return_pct"] is None else f"{r['net_return_pct']:+.2f}"
            mkt = "—" if r["market_return_pct"] is None else f"{r['market_return_pct']:+.2f}"
            pos = "—" if r["positive_day_pct"] is None else f"{r['positive_day_pct']:.0f}%"
            lines.append(
                f"| {label} | T+{h} | {r['days']} | {r['avg_hits']:.1f} | {net} | {mkt} | "
                f"{excess} | {pos} | {r['verdict']} | {'是' if r['actionable'] else '否'} |"
            )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    cands = json.loads(Path(args.cands).read_text())
    panels, _, dates = build_panels(load_market(args.cache), args.min_amount_wan)
    result = evaluate(cands, panels, dates, args.status, horizons)
    print(render(result, args.status, horizons))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
