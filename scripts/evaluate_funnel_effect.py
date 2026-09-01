"""漏斗产出的选股效果检验（绝对收益 + 配对对照 + 随机负控制）。

两个不同的问题，必须同时看：

1. **绝对口径**：拿着这批票赚不赚钱？算在全部候选上，扣往返成本，另给一个不做
   任何中性化的指数基准差额，用来分清赚的是 beta 还是 alpha。
2. **配对超额**：同动量同侪里选得好不好？相对「同动量的非候选股」有没有超额。

只看任何一栏都会得出相反的结论。2026-06-01~08-31 这段就是活例子：T+10 配对超额
+2.06pct 看着像有边缘，同期绝对净收益 **-3.35%**、胜率 42%、减基准 -2.81pct——
候选跌得比同动量同侪少，但仍然是亏的，而且比什么都不做还差。

为什么不用 evaluate_gated_regime_candidates.py：它按 regime 切分，每档只剩
8~15 天过不了 MIN_DAYS=20；且基准是同日全市场等权，在本段样本里全市场大跌，
会把「跟跌少一点」读成选股能力。本脚本换成最近邻动量配对，并强制带随机负控制。

用法：
    python scripts/backtest_snapshot_fetch.py --start 2026-01-01 --end 2026-09-01 \
        --board all --output-dir /tmp/funnel_snap
    python scripts/build_funnel_cands.py --output /tmp/funnel_cands.json
    python scripts/evaluate_funnel_effect.py \
        --cands /tmp/funnel_cands.json --cache /tmp/funnel_snap/hist_full.csv.gz \
        --benchmark /tmp/funnel_snap/benchmark_main.csv \
        --status formal_l4 --horizons 5,10,20

候选集必须由 build_funnel_cands.py 生成：L4 成员判定按 candidate_lane 落在
FORMAL_L4_LANES，手搓 ``candidate_status == "formal_l4"`` 会漏掉 stage 已知的那批。
"""

from __future__ import annotations

import argparse
import json

import _bootstrap  # noqa: F401
import pandas as pd

from core.funnel_effect_eval import (
    CONTROL_SEEDS,
    MIN_DAYS,
    MOM_MATCH_TOL_PCT,
    Panels,
    control_gap,
    evaluate_daily,
    summarize_absolute,
    summarize_group,
)
from core.pattern_forward_eval import ROUND_TRIP_COST_PCT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="漏斗选股效果检验")
    parser.add_argument("--cands", required=True, help="候选 JSON：{date: {regime, formal_l4[], all[]}}")
    parser.add_argument("--cache", required=True, help="行情快照 hist_full.csv.gz 或 tushare CSV")
    parser.add_argument(
        "--status",
        default="formal_l4",
        choices=("formal_l4", "all", "l4_vs_rest"),
        help="测哪一层：formal_l4/all 对照非候选股；l4_vs_rest 在宽池内比 L4 与非 L4",
    )
    parser.add_argument("--horizons", default="5,10,20", help="持有期，逗号分隔")
    parser.add_argument("--min-amount-wan", type=float, default=8000.0, help="20 日均额下限（万元）")
    parser.add_argument("--benchmark", default="", help="基准指数 CSV（快照的 benchmark_main.csv），缺省则不出基准差额")
    parser.add_argument("--json-out", default="", help="结构化结果输出路径")
    return parser.parse_args()


def load_market(path: str) -> pd.DataFrame:
    """兼容两种列名：快照的 date/symbol/amount(元)，与 tushare 的 trade_date/ts_code/amount(千元)。"""
    head = pd.read_csv(path, nrows=1)
    if "symbol" in head.columns:
        frame = pd.read_csv(path, usecols=["date", "open", "close", "amount", "symbol"])
        frame["code"] = frame.symbol.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
        frame["ds"] = pd.to_datetime(frame.date).dt.strftime("%Y-%m-%d")
        frame["amt_wan"] = pd.to_numeric(frame.amount, errors="coerce") / 1e4
    else:
        frame = pd.read_csv(path, usecols=["ts_code", "trade_date", "open", "close", "amount"])
        frame["code"] = frame.ts_code.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
        frame["ds"] = pd.to_datetime(frame.trade_date.astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
        frame["amt_wan"] = pd.to_numeric(frame.amount, errors="coerce") / 10.0
    for col in ("open", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.dropna(subset=["open", "close"]).sort_values(["code", "ds"])


def load_benchmark(path: str) -> tuple[dict[str, float], dict[str, float]]:
    """基准指数的 (开盘, 收盘)。窗口要与候选完全一致，所以两头都要。"""
    if not path:
        return {}, {}
    frame = pd.read_csv(path, usecols=["date", "open", "close"])
    frame["ds"] = pd.to_datetime(frame.date).dt.strftime("%Y-%m-%d")
    for col in ("open", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["open", "close"]).drop_duplicates("ds")
    return (
        dict(zip(frame.ds, frame.open.astype(float), strict=True)),
        dict(zip(frame.ds, frame.close.astype(float), strict=True)),
    )


def build_panels(frame: pd.DataFrame, min_amount_wan: float, benchmark: str = "") -> Panels:
    """流动性池与 20 日动量都用 shift(1)，只含 T 日收盘可知的信息。"""
    frame = frame.copy()
    grouped = frame.groupby("code", sort=False)
    frame["avg20"] = grouped.amt_wan.transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
    # 20 日涨幅按 T 日收盘算（含 T 日），配对时对候选和对照同口径，无前视。
    frame["mom20"] = grouped.close.transform(lambda s: 100.0 * (s / s.shift(20) - 1.0))
    liquid = frame[frame.avg20 >= min_amount_wan]
    bench_open, bench_close = load_benchmark(benchmark)
    return Panels(
        open={ds: dict(zip(g.code, g.open, strict=True)) for ds, g in frame.groupby("ds", sort=False)},
        close={ds: dict(zip(g.code, g.close, strict=True)) for ds, g in frame.groupby("ds", sort=False)},
        liquid={ds: set(g.code) for ds, g in liquid.groupby("ds", sort=False)},
        mom20={
            ds: dict(zip(g.code, g.mom20, strict=True))
            for ds, g in frame.dropna(subset=["mom20"]).groupby("ds", sort=False)
        },
        dates=sorted(frame.ds.unique()),
        bench_open=bench_open,
        bench_close=bench_close,
    )


CONTROL_DESC = {
    "l4_vs_rest": "对照池：同日宽池内未进 L4 的候选（两组都过了宽池入口，差别只在 L4 这道筛）",
}
DEFAULT_CONTROL_DESC = "对照池：同日流动性池内的非候选股"


def read_absolute_notes(result: dict) -> list[str]:
    """绝对口径的读法。放在超额结论之前——超额为正而仓位实亏时，先说的那句才算。"""
    losing = [h for h, b in result["horizons"].items() if (b["absolute"]["net_pct"] or 0) < 0]
    lagging = [h for h, b in result["horizons"].items() if (b["absolute"]["bench_excess_pct"] or 0) < 0]
    if not losing:
        return [
            "- 绝对收益为正。若「减基准」为负，赚的是 beta 不是 alpha，"
            "换成买指数能拿到同样的钱且不承担个股风险。",
        ]
    notes = [
        f"- **T+{'/T+'.join(str(h) for h in losing)} 绝对收益为负：这批票拿在手里是亏的。**"
        "配对超额为正只说明「同动量同侪里选得不算差」，不说明赚钱——"
        "对照亏得更多而已，仓位实亏照样是实亏。",
    ]
    if lagging:
        notes.append(
            f"- T+{'/T+'.join(str(h) for h in lagging)} 还跑输基准：同期什么都不做（买主指数）"
            "亏得更少，这批票连 beta 都没赚到。",
        )
    return notes


def render_absolute(result: dict) -> list[str]:
    """绝对收益块。与超额块分开，因为它回答的是完全不同的问题：拿着赚不赚钱。"""
    lines = [
        "## 绝对收益（不做任何中性化）",
        "",
        "算在**全部候选**上（不是配对子集），已扣往返成本。基准为主指数同窗口 T+1 开盘进、T+1+H 收盘出，不扣成本。",
        "",
        "| H | 天数 | 日均只数 | 绝对净收益 | t | 胜率 | 最差日 | 最好日 | 基准 | 减基准 | t | 判定 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for h, block in result["horizons"].items():
        a = block["absolute"]
        if a["net_pct"] is None:
            lines.append(f"| T+{h} | {a['days']} | — | — | — | — | — | — | — | — | — | 样本不足(<{MIN_DAYS}) |")
            continue
        bench = "—" if a["bench_pct"] is None else f"{a['bench_pct']:+.2f}%"
        b_ex = "—" if a["bench_excess_pct"] is None else f"{a['bench_excess_pct']:+.2f}pct"
        b_t = "—" if a["bench_excess_t"] is None else f"{a['bench_excess_t']:+.2f}"
        lines.append(
            f"| T+{h} | {a['days']} | {a['avg_size']:.1f} | {a['net_pct']:+.2f}% | {a['net_t']:+.2f} "
            f"| {a['positive_day_pct']:.0f}% | {a['worst_day_pct']:+.2f}% | {a['best_day_pct']:+.2f}% "
            f"| {bench} | {b_ex} | {b_t} | {a['verdict']} |"
        )
    return lines


def render(result: dict, status: str) -> str:
    lines = [
        f"# 漏斗选股效果（status={status}）",
        "",
        f"买点 T+1 开盘 / 卖点 T+1+H 收盘 / 两边同扣往返成本 {ROUND_TRIP_COST_PCT}% / 按交易日等权",
        CONTROL_DESC.get(status, DEFAULT_CONTROL_DESC),
        "配对方式：T 日已知 20 日涨幅最近邻 1:1 无放回",
        "",
        *render_absolute(result),
        "",
        "## 配对超额（同动量中性化后）",
        "",
        "| H | 天数 | 日均配对 | 候选净收益 | 配对对照 | 配对超额 | t | 为正日 | 残差动量 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for h, block in result["horizons"].items():
        m = block["matched"]
        if m["excess_pct"] is None:
            lines.append(f"| T+{h} | {m['days']} | — | — | — | — | — | — | 样本不足(<{MIN_DAYS}) |")
            continue
        lines.append(
            f"| T+{h} | {m['days']} | {m['avg_size']:.1f} | {m['net_pct']:+.2f}% | {m['control_pct']:+.2f}% "
            f"| {m['excess_pct']:+.2f}pct | {m['excess_t']:+.2f} | {m['positive_day_pct']:.0f}% "
            f"| {m['residual_mom_pct']:+.2f}pct |"
        )
    lines += [
        "",
        f"## 随机负控制（{len(CONTROL_SEEDS)} 个种子，逐只在同动量 ±{MOM_MATCH_TOL_PCT:.0f}pct 邻域内随机替换）",
        "",
    ]
    inside = 0
    for h, block in result["horizons"].items():
        gap = block["control_gap"]
        if gap.get("verdict") == "样本不足":
            lines.append(f"- T+{h}：样本不足")
            continue
        inside += "落在" in gap["verdict"]
        lines.append(
            f"- T+{h}：配对超额 {gap['matched_excess']:+.3f}pct，"
            f"随机控制 {gap['control_excess_min']:+.3f}~{gap['control_excess_max']:+.3f}pct"
            f"（均值 {gap['control_excess_avg']:+.3f}，宽度 {gap['seed_spread']:.3f}），"
            f"差距 {gap['gap']:+.3f}pct → {gap['verdict']}"
        )
    lines += ["", "## 怎么读", "", *read_absolute_notes(result)]
    if inside:
        lines += [
            f"- **{inside} 个持有期的配对超额落在随机负控制区间内。**上表的超额和 t 值不能读成选股能力：",
            "  同动量邻域内随机抽同样只数就能拿到同等甚至更高的超额，说明这部分收益来自"
            "「候选站在动量轴的哪个位置」，而不是「在同一位置上挑中了哪一只」。",
            "- 为正日比例高（如 70%/75%）同样不构成证据——随机控制的为正日比例一样高。",
        ]
    else:
        lines.append("- 配对超额跑赢随机负控制，含独立选股信息；仍需在更长样本上按走前口径复核后才可据此改阈值。")
    lines += [
        f"- 单次往返成本 {ROUND_TRIP_COST_PCT}%：超额小于它时，净收益上看不出差别。",
        "- 残差动量列是配对是否真中性化的检查项；它偏离 0 太多时，超额里混着动量差，整行作废。",
        "- **所有 t 值都被高估**：逐日观测的持有窗口互相重叠（T+H 每天与相邻 H 天共用行情），"
        "有效样本远小于天数。要判显著性得取不重叠日并扫遍相位，本表的 t 只当方向参考，"
        "不能拿来宣布「显著」或「不显著」。",
        "- 本口径只回答「同动量同侪里选得好不好」，不回答「该不该在这个市场里买」——后者是水温闸门的事。",
    ]
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    cands = json.load(open(args.cands, encoding="utf-8"))
    panels = build_panels(load_market(args.cache), args.min_amount_wan, args.benchmark)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]

    result: dict = {"status": args.status, "horizons": {}}
    for h in horizons:
        rows = evaluate_daily(cands, panels, h, status=args.status)
        matched = summarize_group("matched", rows["matched"])
        controls = [summarize_group(f"control_{s}", rows[f"control_{s}"]) for s in CONTROL_SEEDS]
        result["horizons"][h] = {
            "absolute": summarize_absolute(rows["absolute"]).as_dict(),
            "matched": matched.as_dict(),
            "controls": [c.as_dict() for c in controls],
            "control_gap": control_gap(matched, controls),
        }

    print(render(result, args.status))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
