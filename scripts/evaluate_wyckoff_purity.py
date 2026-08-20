"""威科夫纯度检验：跑数、落盘、推飞书。

回答两个问题：
1. 本仓与威科夫原版差了多少（原版事件覆盖率）。
2. 差的那部分是正收益还是负收益（原生事件 vs 均线叠加，跨 T+5/10/20/40）。

首轮（2026-08-20，498 个交易日 / 269 万行）：**偏离原版的方向是对的，补全会更亏**。
唯一正 alpha 是 SOS+MA200+T+5（+0.43、57% 日为正），而原版缺失的 SC 抛售高潮
T+40 超额 -3.83pct。详见 core/wyckoff_purity_eval.py 的模块文档。

事件定义刻意用**朴素价量口径**而非复用生产代码：生产的 spring/sos 判定本身已内嵌均线与
评分，用它测「均线的影响」会循环论证。这里的定义只依赖高低点、收盘位置与量比。

用法::

    python scripts/evaluate_wyckoff_purity.py --start 2024-08-01
    python scripts/evaluate_wyckoff_purity.py --start 2025-06-01 --no-notify
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd

from core.wyckoff_purity_eval import (
    HORIZONS,
    MIN_GROUP,
    ROUND_TRIP_COST_PCT,
    EventCurve,
    canon_coverage,
    summarize_horizon,
)

WARMUP = 210


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="威科夫纯度检验")
    parser.add_argument("--start", default="2024-08-01", help="行情起始（需留足 210 日预热）")
    parser.add_argument("--out", default="docs/evidence", help="产物目录")
    parser.add_argument("--no-notify", action="store_true", help="不推飞书")
    return parser.parse_args()


def load_market(start: str) -> pd.DataFrame:
    from integrations.fetch_a_share_csv import cached_trade_dates
    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise SystemExit("需要 TUSHARE_TOKEN")
    end = pd.Timestamp.now().strftime("%Y-%m-%d")
    days = [str(day) for day in cached_trade_dates() if start <= str(day) <= end]
    frames = []
    for day in days:
        try:
            frame = pro.daily(trade_date=day.replace("-", ""), fields="ts_code,trade_date,high,low,close,vol")
            if frame is not None and not frame.empty:
                frames.append(frame)
        except Exception as exc:  # noqa: BLE001 - 单日失败不中断整体检验
            print(f"[purity] {day} 取数失败: {str(exc)[:60]}")
    if not frames:
        raise SystemExit("未取到行情")
    market = pd.concat(frames)
    market["d"] = pd.to_datetime(market.trade_date, format="%Y%m%d")
    return market.sort_values(["ts_code", "d"])


def _event_masks(frame: pd.DataFrame) -> dict[str, pd.Series]:
    """朴素价量口径的威科夫事件。不引用生产判定，避免循环论证。"""
    vol_ratio = frame.v / frame.v20
    above200 = frame.c > frame.ma200
    spring = (frame.lowday <= frame.lo20 * 1.002) & (frame.c > frame.lo20) & (frame.cpos >= 50)
    sos = (frame.c >= frame.hi20 * 0.999) & (vol_ratio >= 1.5)
    lps = (frame.c > frame.lo20 * 1.02) & (frame.c < frame.hi20 * 0.97) & (vol_ratio <= 0.8)
    # 原版缺失的两个事件，补测其是否值得实现。
    selling_climax = (frame.lowday <= frame.lo60 * 1.002) & (vol_ratio >= 2.0) & (frame.cpos >= 40)
    secondary_test = (frame.lowday <= frame.lo60 * 1.05) & (frame.lowday > frame.lo60) & (vol_ratio <= 0.7)
    return {
        "Spring": spring,
        "SOS": sos,
        "LPS": lps,
        "SC抛售高潮(原版缺失)": selling_climax,
        "ST二次测试(原版缺失)": secondary_test,
        "SOS+MA200": sos & above200,
        "Spring+MA200下方": spring & ~above200,
    }


def build_curves(market: pd.DataFrame) -> list[EventCurve]:
    close = market.pivot_table(index="d", columns="ts_code", values="close")
    high = market.pivot_table(index="d", columns="ts_code", values="high")
    low = market.pivot_table(index="d", columns="ts_code", values="low")
    volume = market.pivot_table(index="d", columns="ts_code", values="vol")
    dates = list(close.index)
    sink: dict[str, dict[int, list[dict[str, float]]]] = {}

    for i in range(WARMUP, len(dates) - max(HORIZONS)):
        spot = close.iloc[i]
        day_range = high.iloc[i] - low.iloc[i]
        frame = pd.DataFrame(
            {
                "c": spot,
                "ma200": close.iloc[i - 199 : i + 1].mean(),
                "lo20": low.iloc[i - 19 : i + 1].min(),
                "hi20": high.iloc[i - 19 : i + 1].max(),
                "lo60": low.iloc[i - 59 : i + 1].min(),
                "lowday": low.iloc[i],
                "v": volume.iloc[i],
                "v20": volume.iloc[i - 19 : i + 1].mean(),
                "cpos": (spot - low.iloc[i]) / day_range.where(day_range > 0) * 100.0,
            }
        ).dropna()
        frame = frame[(frame.c > 0) & (frame.ma200 > 0) & (frame.lo20 > 0) & (frame.lo60 > 0) & (frame.v20 > 0)]
        if len(frame) < 100:
            continue
        masks = _event_masks(frame)
        for horizon in HORIZONS:
            forward = (close.iloc[i + horizon] / frame.c - 1.0) * 100.0
            scoped = frame.assign(fwd=forward).dropna(subset=["fwd"])
            if len(scoped) < 100:
                continue
            market_ret = float(scoped.fwd.mean())
            for name, mask in masks.items():
                hit = scoped[mask.reindex(scoped.index, fill_value=False)]
                if len(hit) < MIN_GROUP:
                    continue
                sink.setdefault(name, {}).setdefault(horizon, []).append(
                    {"event": float(hit.fwd.mean()), "market": market_ret, "hits": float(len(hit))}
                )

    curves = []
    for name, per_horizon in sink.items():
        curve = EventCurve(name=name, is_canon="原版缺失" not in name and "+" not in name)
        curve.stats = [summarize_horizon(h, per_horizon.get(h, [])) for h in HORIZONS]
        curves.append(curve)
    return curves


def render(curves: list[EventCurve], coverage: dict) -> str:
    lines = [
        "**威科夫纯度检验**",
        "",
        f"**原版事件覆盖** {coverage['coverage_pct']}%（{len(coverage['implemented'])}/{coverage['total']}）　"
        f"已实现：{'、'.join(coverage['implemented'])}",
        f"**未实现**：{'、'.join(coverage['missing'])}",
        "",
        "| 事件 | " + " | ".join(f"T+{h}" for h in HORIZONS) + " | 衰减 |",
        "| --- | " + " | ".join("--:" for _ in HORIZONS) + " | --- |",
    ]
    for curve in sorted(curves, key=lambda c: -(c.best.excess if c.best and c.best.excess else -9)):
        cells = []
        for horizon in HORIZONS:
            stat = curve.at(horizon)
            if stat is None or stat.excess is None:
                cells.append("—")
            else:
                cells.append(f"{stat.excess:+.2f}({stat.positive_day_pct:.0f}%)")
        lines.append(f"| {curve.name} | " + " | ".join(cells) + f" | {'是' if curve.decays else '否'} |")
    lines += [
        "",
        "**读法**　单元格为「超额pct(为正交易日占比)」。超额已扣同日全市场均值。",
        "",
        "**接下来做什么**",
        *_actions(curves, coverage),
    ]
    return "\n".join(lines)


def _actions(curves: list[EventCurve], coverage: dict) -> list[str]:
    out = []
    winners = [c for c in curves if c.best and c.best.excess and c.best.excess > 0]
    if winners:
        top = max(winners, key=lambda c: c.best.excess)
        stat = top.best
        cost_note = "已过成本门槛" if stat.beats_cost else f"**未过成本门槛 {ROUND_TRIP_COST_PCT}%**"
        out.append(
            f"- ① 最佳组合 `{top.name}` 在 T+{stat.horizon} 超额 {stat.excess:+.2f}pct"
            f"（{stat.positive_day_pct:.0f}% 日为正），{cost_note}。"
        )
        decaying = [c.name for c in winners if c.decays]
        if decaying:
            out.append(f"- ② 以下事件的 alpha 随持有期衰减，**不要靠拉长持有期放大**：{'、'.join(decaying)}。")
    canon_missing = [c for c in curves if "原版缺失" in c.name and c.best and c.best.excess is not None]
    negatives = [c.name for c in canon_missing if c.best.excess < 0]
    if negatives:
        out.append(
            f"- ③ 原版缺失事件实测为负贡献：{'、'.join(negatives)}——"
            f"**补全威科夫原版不会带来正收益**，当前 {coverage['coverage_pct']}% 覆盖率不是缺陷。"
        )
    out.append(
        "- ④ 任何据此改动都需增益大于往返成本 0.202%，且跨越多个行情段后方向稳定；"
        "注意区分 beta 与 alpha——绝对收益为正而超额为负意味着只赚了市场的钱。"
    )
    return out


def main() -> int:
    args = parse_args()
    market = load_market(args.start)
    print(f"[purity] 行情 {len(market):,} 行 / {market.ts_code.nunique()} 只")
    curves = build_curves(market)
    coverage = canon_coverage()
    payload = {
        "canon_coverage": coverage,
        "horizons": list(HORIZONS),
        "curves": [curve.as_dict() for curve in curves],
        "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "wyckoff_purity.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    text = render(curves, coverage)
    print(text)
    if not args.no_notify:
        _notify(text)
    return 0


def _notify(markdown: str) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[purity] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    from utils.feishu import send_feishu_notification

    title = f"威科夫纯度检验｜{pd.Timestamp.now().strftime('%Y-%m-%d')}"
    print("[purity] feishu sent" if send_feishu_notification(webhook, title, markdown) else "[purity] feishu failed")


if __name__ == "__main__":
    raise SystemExit(main())
