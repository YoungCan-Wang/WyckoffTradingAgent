"""漏斗产出的选股效果检验（配对对照 + 随机负控制）。

回答的问题：漏斗每天挑出来的那批票，相对「同动量的非候选股」有没有超额？

为什么不用 evaluate_gated_regime_candidates.py：它按 regime 切分，每档只剩
8~15 天过不了 MIN_DAYS=20；且基准是同日全市场等权，在本段样本里全市场大跌，
会把「跟跌少一点」读成选股能力。本脚本换成最近邻动量配对，并强制带随机负控制。

用法：
    python scripts/backtest_snapshot_fetch.py --start 2026-01-01 --end 2026-09-01 \
        --board all --output-dir /tmp/funnel_snap
    python scripts/build_funnel_cands.py --output /tmp/funnel_cands.json
    python scripts/evaluate_funnel_effect.py \
        --cands /tmp/funnel_cands.json --cache /tmp/funnel_snap/hist_full.csv.gz \
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


def build_panels(frame: pd.DataFrame, min_amount_wan: float) -> Panels:
    """流动性池与 20 日动量都用 shift(1)，只含 T 日收盘可知的信息。"""
    frame = frame.copy()
    grouped = frame.groupby("code", sort=False)
    frame["avg20"] = grouped.amt_wan.transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
    # 20 日涨幅按 T 日收盘算（含 T 日），配对时对候选和对照同口径，无前视。
    frame["mom20"] = grouped.close.transform(lambda s: 100.0 * (s / s.shift(20) - 1.0))
    liquid = frame[frame.avg20 >= min_amount_wan]
    return Panels(
        open={ds: dict(zip(g.code, g.open, strict=True)) for ds, g in frame.groupby("ds", sort=False)},
        close={ds: dict(zip(g.code, g.close, strict=True)) for ds, g in frame.groupby("ds", sort=False)},
        liquid={ds: set(g.code) for ds, g in liquid.groupby("ds", sort=False)},
        mom20={
            ds: dict(zip(g.code, g.mom20, strict=True))
            for ds, g in frame.dropna(subset=["mom20"]).groupby("ds", sort=False)
        },
        dates=sorted(frame.ds.unique()),
    )


CONTROL_DESC = {
    "l4_vs_rest": "对照池：同日宽池内未进 L4 的候选（两组都过了宽池入口，差别只在 L4 这道筛）",
}
DEFAULT_CONTROL_DESC = "对照池：同日流动性池内的非候选股"


def render(result: dict, status: str) -> str:
    lines = [
        f"# 漏斗选股效果（status={status}）",
        "",
        f"买点 T+1 开盘 / 卖点 T+1+H 收盘 / 两边同扣往返成本 {ROUND_TRIP_COST_PCT}% / 按交易日等权",
        CONTROL_DESC.get(status, DEFAULT_CONTROL_DESC),
        "配对方式：T 日已知 20 日涨幅最近邻 1:1 无放回",
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
    lines += ["", "## 怎么读", ""]
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
        "- 本口径只回答「同动量同侪里选得好不好」，不回答「该不该在这个市场里买」——后者是水温闸门的事。",
    ]
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    cands = json.load(open(args.cands, encoding="utf-8"))
    panels = build_panels(load_market(args.cache), args.min_amount_wan)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]

    result: dict = {"status": args.status, "horizons": {}}
    for h in horizons:
        rows = evaluate_daily(cands, panels, h, status=args.status)
        matched = summarize_group("matched", rows["matched"])
        controls = [summarize_group(f"control_{s}", rows[f"control_{s}"]) for s in CONTROL_SEEDS]
        result["horizons"][h] = {
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
