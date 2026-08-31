"""动量体检：RPS 闸门的选择价值、阈值扫描、以及动态切换设计的走前验证。

首轮（2026-08-31，402 个交易日 2025-01-02..2026-08-28）结论：**动量没坏，是平的**。
闸门超额 H=5 +0.05pct（t=+0.31）、H=10 +0.07pct（t=+0.28）；2026Q3 的 -6.63 是
正常摆幅的一侧，不是故障。四种动态切换设计走前全部失效。详见 core/momentum_regime_eval.py。

留下这个脚本是为了持续确认该结论，以及在闸门真的转为持续为负时能被看见。

RPS 慢腿要 120 日回看，因此 ``--start`` 之后的头 120 个交易日不可用——想覆盖
N 天就要多取 120 天，默认 start 已按此留足。

用法::

    python scripts/evaluate_momentum_regime.py --horizon 10,5
    python scripts/evaluate_momentum_regime.py --horizon 5 --start 2025-01-01 --no-notify

取数（约 400 次 tushare 日调用）是最贵的一步，多个 horizon 复用同一份行情。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd

from core.momentum_regime_eval import (
    MID_BAND,
    MIN_AMOUNT_RAW,
    MIN_DOMAIN_SIZE,
    MIN_GROUP,
    PROD_RPS_FAST_MIN,
    PROD_RPS_SLOW_MIN,
    PROD_RPS_WINDOW_FAST,
    PROD_RPS_WINDOW_SLOW,
    ROUND_TRIP_COST_PCT,
    THRESHOLD_GRID,
    MomentumReport,
    ic_persistence,
    render,
    summarize_band,
    walk_forward_switch,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="动量体检：RPS 闸门与动态切换走前验证")
    # 逗号分隔可一次跑多个持有期。取数是这里最贵的一步（约 400 次 tushare 日调用），
    # 多个 horizon 复用同一份行情，避免重复拉取。
    parser.add_argument("--horizon", default="10,5", help="前瞻交易日数，逗号分隔")
    parser.add_argument("--start", default="2025-01-01", help="行情起始（RPS 慢腿需 120 日预热）")
    parser.add_argument("--out", default="docs/evidence", help="产物目录")
    parser.add_argument("--no-notify", action="store_true", help="不推飞书")
    return parser.parse_args()


def load_market(start: str) -> pd.DataFrame:
    """全市场日线。按交易日批量取，逐只取会慢一个数量级。"""
    from integrations.fetch_a_share_csv import cached_trade_dates
    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise SystemExit("需要 TUSHARE_TOKEN")
    end = pd.Timestamp.now().strftime("%Y-%m-%d")
    days = [str(day) for day in cached_trade_dates() if start <= str(day) <= end]
    frames = []
    for day in days:
        frame = _fetch_day(pro, day)
        if frame is not None and not frame.empty:
            frames.append(frame)
    if not frames:
        raise SystemExit("未取到行情")
    market = pd.concat(frames)
    market["trade_date"] = market["trade_date"].astype(int)
    return market


def _fetch_day(pro, day: str) -> pd.DataFrame | None:
    """单日全市场。tushare 限频，失败重试几次；单日缺失不应中断整体检验。"""
    fields = "ts_code,trade_date,open,close,amount"
    last = "未知错误"
    for _ in range(4):
        try:
            return pro.daily(trade_date=day.replace("-", ""), fields=fields)
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:60]
            time.sleep(0.14)
    print(f"[mom] {day} 取数失败: {last}")
    return None


def build_panels(market: pd.DataFrame, horizon: int) -> tuple[dict[str, list[dict[str, float]]], list[float]]:
    """逐日算各档动量带的前向收益，以及当日动量 RankIC。

    返回 (各档逐日观测, 逐日 IC)。前向收益按 T+1 开盘买、T+1+H 收盘卖，扣往返成本。
    """
    close = market.pivot(index="trade_date", columns="ts_code", values="close").sort_index()
    open_ = market.pivot(index="trade_date", columns="ts_code", values="open").sort_index()
    amount = market.pivot(index="trade_date", columns="ts_code", values="amount").sort_index()
    ret_fast = close / close.shift(PROD_RPS_WINDOW_FAST) - 1.0
    ret_slow = close / close.shift(PROD_RPS_WINDOW_SLOW) - 1.0
    liquidity = amount.rolling(20).mean()
    dates = list(close.index)

    sink: dict[str, list[dict[str, float]]] = {}
    switch_rows: list[dict[str, float]] = []
    daily_ic: list[float] = []
    for i in range(PROD_RPS_WINDOW_SLOW, len(dates) - horizon - 1):
        snap = _day_snapshot(close, open_, liquidity, ret_fast, ret_slow, i, horizon)
        if snap is None:
            continue
        _collect_day(snap, int(dates[i]), sink, switch_rows, daily_ic)
    sink["__switch__"] = switch_rows
    return sink, daily_ic


def _day_snapshot(close, open_, liquidity, ret_fast, ret_slow, i: int, horizon: int):
    """单日截面：流动性域内的动量分位与前向收益。不足样本量返回 None。"""
    tradable = liquidity.iloc[i]
    domain = tradable[tradable >= MIN_AMOUNT_RAW].index
    fast = ret_fast.iloc[i].reindex(domain).dropna()
    slow = ret_slow.iloc[i].reindex(domain).dropna()
    forward = ((close.iloc[i + 1 + horizon] / open_.iloc[i + 1] - 1.0) * 100.0 - ROUND_TRIP_COST_PCT).dropna()
    common = fast.index.intersection(slow.index).intersection(forward.index)
    if len(common) < MIN_DOMAIN_SIZE:
        return None
    return (
        fast[common].rank(pct=True) * 100.0,
        slow[common].rank(pct=True) * 100.0,
        forward[common],
    )


def _collect_day(snap, date: int, sink, switch_rows, daily_ic) -> None:
    pct_fast, pct_slow, forward = snap
    domain_ret = float(forward.mean())
    daily_ic.append(float(pct_slow.rank().corr(forward.rank())))
    sink.setdefault("__domain__", []).append(
        {"date": date, "inside": domain_ret, "domain": domain_ret, "size": float(len(forward))}
    )
    for fast_min, slow_min in THRESHOLD_GRID:
        mask = (pct_fast >= fast_min) & (pct_slow >= slow_min)
        if int(mask.sum()) < MIN_GROUP:
            continue
        sink.setdefault(f"{fast_min:.0f}/{slow_min:.0f}", []).append(
            {"date": date, "inside": float(forward[mask].mean()), "domain": domain_ret, "size": float(mask.sum())}
        )
    flo, fhi, slo, shi = MID_BAND
    mid = (pct_fast >= flo) & (pct_fast < fhi) & (pct_slow >= slo) & (pct_slow < shi)
    gate = (pct_fast >= PROD_RPS_FAST_MIN) & (pct_slow >= PROD_RPS_SLOW_MIN)
    if int(mid.sum()) >= MIN_GROUP:
        sink.setdefault("中动量档", []).append(
            {"date": date, "inside": float(forward[mid].mean()), "domain": domain_ret, "size": float(mid.sum())}
        )
        if int(gate.sum()) >= MIN_GROUP:
            switch_rows.append(
                {
                    "date": date,
                    "gate": float(forward[gate].mean()),
                    "mid": float(forward[mid].mean()),
                    "breadth": float((forward > 0).mean()),
                    "dispersion": float(forward.std()),
                }
            )


def build_report(sink: dict[str, list[dict[str, float]]], daily_ic: list[float], horizon: int) -> MomentumReport:
    report = MomentumReport()
    for fast_min, slow_min in THRESHOLD_GRID:
        label = f"{fast_min:.0f}/{slow_min:.0f}"
        is_prod = fast_min == PROD_RPS_FAST_MIN and slow_min == PROD_RPS_SLOW_MIN
        report.thresholds.append(summarize_band(label, sink.get(label, []), is_production=is_prod))
    report.mid_band = summarize_band("中动量档 40-65/40-70", sink.get("中动量档", []))
    report.domain = summarize_band("域内基准（对照）", sink.get("__domain__", []))
    rows = sink.get("__switch__", [])
    report.switches = [
        walk_forward_switch("按市场宽度切换", rows, state_key="breadth", high_is_on=True),
        walk_forward_switch("按截面离散度切换", rows, state_key="dispersion", high_is_on=False),
    ]
    report.ic_persistence = ic_persistence(daily_ic, horizon)
    return report


def _parse_horizons(raw: str) -> list[int]:
    seen: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        value = max(int(part), 1)
        if value not in seen:
            seen.append(value)
    return seen or [10]


def run_one(market: pd.DataFrame, horizon: int, out_dir: Path, start: str) -> str:
    sink, daily_ic = build_panels(market, horizon)
    report = build_report(sink, daily_ic, horizon)
    payload = report.as_dict()
    payload["horizon_days"] = horizon
    payload["start"] = start
    out_path = out_dir / f"momentum_regime_h{horizon}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[mom] 落盘 {out_path}")
    text = render(report, horizon)
    print(text)
    return text


def main() -> int:
    args = parse_args()
    horizons = _parse_horizons(args.horizon)
    market = load_market(args.start)
    print(f"[mom] 行情 {len(market):,} 行 / {market.ts_code.nunique()} 只 / {market.trade_date.nunique()} 天")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    sections = [run_one(market, horizon, out_dir, args.start) for horizon in horizons]
    if not args.no_notify:
        # 多持有期合成一条推送：H=5 与 H=10 的季度符号是否一致要放在一起看。
        _notify("\n\n---\n\n".join(sections), horizons)
    return 0


def _notify(markdown: str, horizons: list[int]) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[mom] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    from utils.feishu import send_feishu_notification

    spans = "/".join(f"T+{h}" for h in horizons)
    title = f"动量体检｜{spans}｜{pd.Timestamp.now().strftime('%Y-%m-%d')}"
    print("[mom] feishu sent" if send_feishu_notification(webhook, title, markdown) else "[mom] feishu failed")


if __name__ == "__main__":
    raise SystemExit(main())
