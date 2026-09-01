"""动量体检：RPS 闸门的选择价值、阈值扫描、以及动态切换设计的走前验证。

⚠️ **``--start`` 不是评估区间。**RPS 慢腿 120 日预热把起点往后推 120 个交易日，尾部
再扣 H+1 天：取 402 天行情只评估了 271 天（2025-07-04 起），首轮标的「402 个交易日
2025-01-02..2026-08-28」是一个从未被评估过的区间。产物里的 ``eval_window`` 是唯一
可信的样本范围，别拿 ``--start`` 当它读。

这不是措辞问题，两个窗口给的是相反结论（同一份行情、H=10、生产档 65/70）：

    271 个评估日（start=2025-01）  超额 +0.070  t=+0.28  期望为零：无选择价值
    513 个评估日（start=2024-01）  超额 -0.320  t=-2.08  负贡献

H=5 同向（+0.050 → -0.151）。所以默认 start 从 2025-01-01 提到 2024-01-01：短窗看不
见的恰恰是这个体检要看见的东西。

留下这个脚本是为了持续确认闸门方向，以及在它真的转为持续为负时能被看见。

用法::

    python scripts/evaluate_momentum_regime.py --horizon 10,5
    python scripts/evaluate_momentum_regime.py --horizon 5 --start 2024-01-01 --no-notify
    # 已有 backtest 快照时直接复用，省掉逐日取数
    python scripts/evaluate_momentum_regime.py --horizon 10 --cache /tmp/snap/hist_full.csv.gz

取数是最贵的一步（逐日约 1.6s/天，893 天约 24 分钟），多个 horizon 复用同一份行情。
``--cache`` 可改读 backtest 快照，注意两种来源的 amount 单位差 1000 倍，脚本会归一。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd

from core.momentum_regime_eval import (
    CONTROL_SEEDS,
    MID_BAND,
    MIN_AMOUNT_RAW,
    MIN_DOMAIN_SIZE,
    MIN_GROUP,
    NON_TOP_CAP,
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
    # 2024-01-01 给约 513 个评估日。原默认 2025-01-01 只剩 271 天，把「负贡献」读成
    # 「期望为零」——预热吃掉的半年不在评估区间里。
    parser.add_argument("--start", default="2024-01-01", help="行情起始（预热后实际评估区间见 eval_window）")
    # 逐日 tushare 取数约 1.6s/天，1128 天要半小时；快照逐只取，拉长几乎不加成本。
    parser.add_argument("--cache", default="", help="行情来源改用 backtest 快照 hist_full.csv.gz，缺省则逐日取 tushare")
    parser.add_argument("--out", default="docs/evidence", help="产物目录")
    parser.add_argument("--no-notify", action="store_true", help="不推飞书")
    return parser.parse_args()


def amount_to_raw_divisor(frame: pd.DataFrame) -> float:
    """把 amount 归一到 tushare 口径（千元），因为 MIN_AMOUNT_RAW 是按它定的。

    两种来源差 1000 倍：tushare ``amount`` 单位千元，``amount/(vol*close)`` 中位数
    约 0.10；backtest 快照 hist_full 单位是元，同一比值约 100。不归一就等于把
    流动性门槛放宽/收紧 1000 倍，域会整个换掉而且不报错。
    """
    probe = frame[["amount", "vol", "close"]].dropna()
    probe = probe[(probe.vol > 0) & (probe.close > 0)]
    if probe.empty:
        return 1.0
    ratio = float((probe.amount / (probe.vol * probe.close)).median())
    # 1.0 是 0.10 与 100 的几何中点，足够把两种来源分开。
    return 1000.0 if ratio >= 1.0 else 1.0


def load_cached_market(cache: str, start: str) -> pd.DataFrame:
    """读 backtest 快照（``hist_full.csv.gz``）当行情源。

    逐日 tushare 取数约 1.6s/天，1128 天要半小时；快照是逐只取的，拉长区间几乎不
    加成本，长样本只有这条路划得来。列名与单位都归一到 tushare 口径。
    """
    frame = pd.read_csv(cache, dtype={"symbol": str, "ts_code": str}, low_memory=False)
    if "ts_code" not in frame.columns and "symbol" in frame.columns:
        frame = frame.rename(columns={"symbol": "ts_code"})
    if "vol" not in frame.columns and "volume" in frame.columns:
        frame = frame.rename(columns={"volume": "vol"})
    if "trade_date" not in frame.columns:
        frame["trade_date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y%m%d")
    missing = {"ts_code", "trade_date", "open", "close", "amount", "vol"} - set(frame.columns)
    if missing:
        raise SystemExit(f"行情缺少必要列: {sorted(missing)}")
    frame = frame.dropna(subset=["trade_date"])
    frame = frame[frame.trade_date.astype(str) >= start.replace("-", "")]
    if frame.empty:
        raise SystemExit(f"快照在 {start} 之后没有数据")
    frame["amount"] = pd.to_numeric(frame.amount, errors="coerce") / amount_to_raw_divisor(frame)
    frame["trade_date"] = frame.trade_date.astype(int)
    return frame


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

    循环从 ``PROD_RPS_WINDOW_SLOW`` 起、到倒数第 ``horizon+1`` 天止，所以**实际评估
    区间比 ``--start`` 晚 120 个交易日、比行情末尾早 H+1 天**。402 天的行情只剩 271
    天可评估，起点从 2025-01-02 推到 2025-07-04。区间靠 ``eval_window`` 显式落盘，
    不要拿 ``--start`` 当样本范围读。
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
    sink["__window__"] = eval_window(dates, horizon)
    return sink, daily_ic


def eval_window(dates: list, horizon: int) -> list[dict[str, float]]:
    """实际评估区间。与 build_panels 的循环边界同源，改一处必然带上另一处。

    单独落盘是因为 ``--start`` 会骗人：预热把起点往后推 120 个交易日，光看它会把
    没评估过的半年当成已覆盖（板块强度那次 IC 复现不了就是这个原因）。
    """
    lo, hi = PROD_RPS_WINDOW_SLOW, len(dates) - horizon - 1
    if hi <= lo:
        return []
    return [
        {
            "market_start": float(dates[0]),
            "eval_start": float(dates[lo]),
            "eval_end": float(dates[hi - 1]),
            "market_days": float(len(dates)),
            "eval_days": float(hi - lo),
        }
    ]


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
        _collect_non_top_control(pct_fast, pct_slow, forward, date, int(mid.sum()), domain_ret, sink)
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


def _collect_non_top_control(pct_fast, pct_slow, forward, date: int, size: int, domain_ret: float, sink) -> None:
    """随机负控制：每天从「两条腿都低于 NON_TOP_CAP 分位」里随机抽 size 只。

    只数与中动量档当日相同，域也只砍掉顶部，因此这一档除了「避开顶部」不含任何
    选择信息。中动量档比不过它，就说明它的边缘同样只是避开顶部。

    随机数按 (seed, 交易日) 定种，同一天同一种子在多次运行、多个 horizon 之间
    都取到同一批票，否则各档之间的差值会掺进抽样噪声。
    """
    eligible = list(forward.index[(pct_fast < NON_TOP_CAP) & (pct_slow < NON_TOP_CAP)])
    if len(eligible) < size:
        return
    for seed in CONTROL_SEEDS:
        picks = random.Random(seed * 1_000_003 + date).sample(eligible, size)
        sink.setdefault(f"__control_{seed}__", []).append(
            {"date": date, "inside": float(forward[picks].mean()), "domain": domain_ret, "size": float(size)}
        )


def build_report(sink: dict[str, list[dict[str, float]]], daily_ic: list[float], horizon: int) -> MomentumReport:
    report = MomentumReport()
    for fast_min, slow_min in THRESHOLD_GRID:
        label = f"{fast_min:.0f}/{slow_min:.0f}"
        is_prod = fast_min == PROD_RPS_FAST_MIN and slow_min == PROD_RPS_SLOW_MIN
        report.thresholds.append(summarize_band(label, sink.get(label, []), is_production=is_prod))
    report.mid_band = summarize_band("中动量档 40-65/40-70", sink.get("中动量档", []))
    report.domain = summarize_band("域内基准（对照）", sink.get("__domain__", []))
    report.controls = [
        summarize_band(f"随机负控制 <{NON_TOP_CAP:.0f} 分位 seed={seed}", sink.get(f"__control_{seed}__", []))
        for seed in CONTROL_SEEDS
    ]
    rows = sink.get("__switch__", [])
    # horizon 传进去,价差 t 才会按不重叠日算——相邻日的 H 日前向收益共用 H-1 天。
    report.switches = [
        walk_forward_switch("按市场宽度切换", rows, state_key="breadth", high_is_on=True, horizon=horizon),
        walk_forward_switch("按截面离散度切换", rows, state_key="dispersion", high_is_on=False, horizon=horizon),
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
    # start 是取数起点，不是评估区间；预热把后者往后推 120 个交易日，两个都要落盘。
    window = (sink.get("__window__") or [{}])[0]
    payload["eval_window"] = {k: int(v) for k, v in window.items()}
    if window:
        print(
            f"[mom] 行情自 {int(window['market_start'])} 起共 {int(window['market_days'])} 天；"
            f"扣掉预热 {PROD_RPS_WINDOW_SLOW} 与 H+1 后实际评估 "
            f"{int(window['eval_start'])}~{int(window['eval_end'])}，{int(window['eval_days'])} 天"
        )
    out_path = out_dir / f"momentum_regime_h{horizon}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[mom] 落盘 {out_path}")
    text = render(report, horizon, payload["eval_window"])
    print(text)
    return text


def main() -> int:
    args = parse_args()
    horizons = _parse_horizons(args.horizon)
    market = load_cached_market(args.cache, args.start) if args.cache else load_market(args.start)
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
