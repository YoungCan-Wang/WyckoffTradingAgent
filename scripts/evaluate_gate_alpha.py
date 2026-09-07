"""门槛层 alpha 检验：跑数、落盘、推飞书。

回答两个问题：
1. L3 按行业动量分组这一层有没有正向选股能力？
2. 止损的跟踪参考价（60 日最高）陈旧到什么程度时，止损开始变成错的？

**2026-09-07：首轮结论作废，见 core/gate_alpha_eval 模块 docstring。**首轮只算了裸日均
差，两侧动量没配平，而两个标签按定义都是动量的函数。补做逐对随机互换否证后：止损那
4 档的「超额全为负」配平动量即落回零分布（胜率差 +0.041pct，p=0.821），不含信息；题材层
「避开热门」通过了（胜率差 +4.845pct，p=0.005）但集中在上半年，7~8 月消失。

判定只对**生产档**做对照（题材 topN=5、止损四档），网格其余行仍只出裸差值、判定里明写
「不是结论」——网格是探索性的，200 次置换 × 全网格跑不动，也没必要。

用法::

    python scripts/evaluate_gate_alpha.py --horizon 5
    python scripts/evaluate_gate_alpha.py --horizon 5 --no-notify
    python scripts/evaluate_gate_alpha.py --horizon 5 --no-control   # 只出裸差值，快
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
import pandas as pd

from core.funnel_effect_eval import (
    MOM_MATCH_TOL_PCT,
    Panels,
    SwapDay,
    match_by_momentum,
    swap_falsification,
)
from core.gate_alpha_eval import (
    MIN_GROUP,
    PROD_RECENT_HIGH_WINDOW,
    PROD_TOP_N_SECTORS,
    PROD_TRAILING_DRAWDOWN_PCT,
    STALE_BANDS,
    TOP_N_GRID,
    GateReport,
    band_of,
    render,
    summarize,
)

WARMUP_BARS = 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="门槛层 alpha 检验")
    parser.add_argument("--horizon", type=int, default=5, help="前瞻交易日数")
    parser.add_argument("--start", default="2025-11-01", help="行情起始（需留足 60 日预热）")
    parser.add_argument("--out", default="docs/evidence", help="产物目录")
    parser.add_argument("--no-notify", action="store_true", help="不推飞书")
    parser.add_argument(
        "--no-control",
        action="store_true",
        help="跳过逐对互换否证，只出裸差值（判定里会明写不是结论）",
    )
    return parser.parse_args()


def load_market(start: str) -> pd.DataFrame:
    """全市场日线。按交易日批量取，逐只取会慢一个数量级。

    ``open`` 是对照要用的：互换否证按 T+1 开盘买入、T+1+H 收盘卖出，与
    ``core/funnel_effect_eval`` 同口径。裸差值那部分仍用收盘→收盘，两者不混在一栏里。
    """
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
            frame = pro.daily(trade_date=day.replace("-", ""), fields="ts_code,trade_date,open,high,close")
            if frame is not None and not frame.empty:
                frames.append(frame)
        except Exception as exc:  # noqa: BLE001 - 单日失败不应中断整体检验
            print(f"[gate] {day} 取数失败: {str(exc)[:60]}")
    if not frames:
        raise SystemExit("未取到行情")
    market = pd.concat(frames)
    market["d"] = pd.to_datetime(market.trade_date, format="%Y%m%d")
    return market.sort_values(["ts_code", "d"])


def build_report(
    market: pd.DataFrame,
    sector_map: dict[str, str],
    horizon: int,
    *,
    with_control: bool = True,
) -> GateReport:
    close = market.pivot_table(index="d", columns="ts_code", values="close")
    high = market.pivot_table(index="d", columns="ts_code", values="high")
    open_ = market.pivot_table(index="d", columns="ts_code", values="open")
    dates = list(close.index)
    theme_daily: dict[int, list[dict[str, float]]] = {top_n: [] for top_n in TOP_N_GRID}
    stop_daily: dict[str, list[dict[str, float]]] = {label: [] for _, _, label in STALE_BANDS}
    # 只给生产档留成员名单：对照要用它配对，网格其余行不做对照。
    theme_members: list[dict[str, Any]] = []
    stop_members: list[dict[str, Any]] = []

    for i in range(WARMUP_BARS, len(dates) - horizon):
        spot = close.iloc[i]
        forward = (close.iloc[i + horizon] / spot - 1.0) * 100.0
        momentum = (spot / close.iloc[i - 5] - 1.0) * 100.0
        frame = pd.DataFrame({"c": spot, "r5": momentum, "fwd": forward}).dropna()
        frame = frame[frame.c > 0]
        if len(frame) < 50:
            continue
        ds = dates[i].strftime("%Y-%m-%d")
        market_ret = float(frame.fwd.mean())
        _collect_theme(frame, sector_map, theme_daily, market_ret, ds, theme_members)
        _collect_stop(frame, high, close, i, stop_daily, market_ret, ds, stop_members)

    panels = _build_panels(open_, close, dates) if with_control else None
    report = GateReport()
    report.theme = [
        summarize(
            f"topN={top_n}",
            theme_daily[top_n],
            is_production=top_n == PROD_TOP_N_SECTORS,
            swap_question=("非热门 vs 热门（动量配平）" if top_n == PROD_TOP_N_SECTORS else None),
            swap=(
                _theme_control(theme_members, panels, horizon)
                if panels is not None and top_n == PROD_TOP_N_SECTORS
                else None
            ),
        )
        for top_n in TOP_N_GRID
    ]
    report.stop_loss = [
        summarize(
            label,
            stop_daily[label],
            swap_question="未触发 vs 已触发（动量配平）",
            swap=(None if panels is None else _stop_control(stop_members, panels, horizon, label)),
        )
        for _, _, label in STALE_BANDS
    ]
    return report


def _build_panels(open_: pd.DataFrame, close: pd.DataFrame, dates: list[Any]) -> Panels:
    """互换否证要的行情面板。只有 open/close/dates 被用到，其余给空。

    ``open_`` 按 ``close`` 的索引对齐再转 dict：某天全市场没有开盘价时两个 pivot 的
    行数会不一样，直接 zip 会把后面所有日期错位一格——那种错位不会报错，只会让整栏
    收益悄悄读错一天。
    """
    keys = [d.strftime("%Y-%m-%d") for d in dates]
    aligned = open_.reindex(index=close.index, columns=close.columns)
    return Panels(
        open=_panel_dict(aligned, keys),
        close=_panel_dict(close, keys),
        liquid={},
        mom20={},
        dates=keys,
    )


def _panel_dict(frame: pd.DataFrame, keys: list[str]) -> dict[str, dict[str, float]]:
    return {
        key: {str(code): float(value) for code, value in row.items() if value == value}
        for key, row in zip(keys, frame.to_dict("records"), strict=True)
    }


def _pairs_oriented(
    tested: list[str],
    control: list[str],
    mom: dict[str, float],
) -> list[tuple[str, str]]:
    """配对并把结果摆成 (待测, 对照)。

    1:1 无放回下配对数上限是**小的那一侧**，所以永远拿小篮子当 hits。反过来（大篮子
    当 hits）既截断样本，又按「动量升序先到先得」决定谁被抽中——抽样规则本身带偏。
    """
    if len(tested) <= len(control):
        return match_by_momentum(tested, control, mom, tol_pct=MOM_MATCH_TOL_PCT)
    return [(t, c) for c, t in match_by_momentum(control, tested, mom, tol_pct=MOM_MATCH_TOL_PCT)]


def _swap_days(
    members: list[dict[str, Any]],
    panels: Panels,
    horizon: int,
    pick: Callable[[dict[str, Any]], tuple[list[str], list[str]] | None],
) -> list[SwapDay]:
    """把逐日成员名单变成配好对的 SwapDay。``pick`` 返回 (待测, 对照)。"""
    out: list[SwapDay] = []
    for row in members:
        sides = pick(row)
        if sides is None:
            continue
        tested, control = sides
        if len(tested) < MIN_GROUP or len(control) < MIN_GROUP:
            continue
        window = panels.window(row["ds"], horizon)
        if window is None:
            continue
        pairs = _pairs_oriented(tested, control, row["mom"])
        if pairs:
            out.append(SwapDay(date=row["ds"], buy_ds=window[0], sell_ds=window[1], pairs=pairs))
    return out


def _theme_control(members: list[dict[str, Any]], panels: Panels, horizon: int) -> dict[str, Any] | None:
    """题材层对照：待测=非热门（要动的那一侧），对照=热门，按 5 日动量配平。

    配对变量取 r5 而不是 r20 是刻意的——「热门」这个标签本身就是成分股 r5 的行业均值，
    拿定义它的那个变量来配平是最紧的一道。
    """
    days = _swap_days(members, panels, horizon, lambda row: (row["cold"], row["hot"]))
    return swap_falsification(days, panels) or None


def _stop_control(members: list[dict[str, Any]], panels: Panels, horizon: int, band: str) -> dict[str, Any] | None:
    """止损层对照：待测=未触发，对照=该档已触发，按 5 日动量配平。

    对照池限定在**这一档内**，回答的是「这一档触发得对不对」。首轮拿未匹配的全市场
    均值当对照，而「深偏离」按定义就是从 60 日高点跌得最多的票，那个超额里主要是动量。
    """

    def pick(row: dict[str, Any]) -> tuple[list[str], list[str]] | None:
        fired = row["bands"].get(band)
        return None if not fired else (row["unfired"], fired)

    days = _swap_days(members, panels, horizon, pick)
    return swap_falsification(days, panels) or None


def _collect_theme(
    frame: pd.DataFrame,
    sector_map: dict[str, str],
    sink: dict[int, list[dict[str, float]]],
    market_ret: float,
    ds: str,
    members: list[dict[str, Any]],
) -> None:
    work = frame.copy()
    work["code"] = [str(x).split(".")[0] for x in work.index]
    work["ind"] = work.code.map(sector_map)
    work = work.dropna(subset=["ind"])
    if work.empty:
        return
    strength = work.groupby("ind").r5.mean().sort_values(ascending=False)
    for top_n in sink:
        hot = set(strength.head(top_n).index)
        inside = work[work.ind.isin(hot)]
        outside = work[~work.ind.isin(hot)]
        if len(inside) >= MIN_GROUP and len(outside) >= MIN_GROUP:
            sink[top_n].append(
                {"inside": float(inside.fwd.mean()), "outside": float(outside.fwd.mean()), "size": float(len(inside))}
            )
            if top_n == PROD_TOP_N_SECTORS:
                members.append(
                    {
                        "ds": ds,
                        "hot": [str(x) for x in inside.index],
                        "cold": [str(x) for x in outside.index],
                        "mom": {str(k): float(v) for k, v in work.r5.items()},
                    }
                )
    del market_ret  # 题材层用「热门 vs 非热门」直接对照，不需要市场基准


def _collect_stop(
    frame: pd.DataFrame,
    high: pd.DataFrame,
    close: pd.DataFrame,
    index: int,
    sink: dict[str, list[dict[str, float]]],
    market_ret: float,
    ds: str,
    members: list[dict[str, Any]],
) -> None:
    """复刻生产止损：trailing = 60日最高 × (1 + drawdown)，并与 MA50×0.98 取高者。"""
    window_high = high.iloc[index - (PROD_RECENT_HIGH_WINDOW - 1) : index + 1].max()
    ma50 = close.iloc[index - 49 : index + 1].mean()
    work = frame.join(pd.DataFrame({"h60": window_high, "ma50": ma50}), how="inner").dropna()
    work = work[(work.h60 > 0) & (work.ma50 > 0)]
    if work.empty:
        return
    trailing = work.h60 * (1.0 + PROD_TRAILING_DRAWDOWN_PCT / 100.0)
    stop_price = pd.concat([trailing, work.ma50 * 0.98], axis=1).max(axis=1)
    hit = work.c <= stop_price
    fired = work[hit].copy()
    if fired.empty:
        return
    fired["dev"] = (fired.h60 / fired.c - 1.0) * 100.0
    fired["band"] = fired.dev.map(band_of)
    bands: dict[str, list[str]] = {}
    for label, group in fired.dropna(subset=["band"]).groupby("band"):
        if len(group) >= MIN_GROUP:
            sink[str(label)].append(
                {"inside": float(group.fwd.mean()), "outside": market_ret, "size": float(len(group))}
            )
            bands[str(label)] = [str(x) for x in group.index]
    if bands:
        members.append(
            {
                "ds": ds,
                "bands": bands,
                "unfired": [str(x) for x in work[~hit].index],
                "mom": {str(k): float(v) for k, v in work.r5.items()},
            }
        )


def main() -> int:
    args = parse_args()
    from integrations.market_metadata import fetch_sector_map

    sector_map = fetch_sector_map()
    print(f"[gate] 行业映射 {len(sector_map)} 只")
    market = load_market(args.start)
    print(f"[gate] 行情 {len(market):,} 行 / {market.ts_code.nunique()} 只")
    report = build_report(market, sector_map, max(int(args.horizon), 1), with_control=not args.no_control)
    payload = report.as_dict()
    payload["horizon_days"] = int(args.horizon)
    payload["control_ran"] = not args.no_control
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"gate_alpha_h{args.horizon}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = render(report)
    print(text)
    if not args.no_notify:
        _notify(text, int(args.horizon))
    return 0


def _notify(markdown: str, horizon: int) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[gate] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    from utils.feishu import send_feishu_notification

    title = f"门槛层 alpha 检验｜T+{horizon}｜{pd.Timestamp.now().strftime('%Y-%m-%d')}"
    print("[gate] feishu sent" if send_feishu_notification(webhook, title, markdown) else "[gate] feishu failed")


if __name__ == "__main__":
    raise SystemExit(main())
