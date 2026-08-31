"""排序权重体检：跑数、落盘、推飞书。

回答一个问题：``watch_score`` 里的 ``dry_q``（生产权重 0.20）值不值这个分量，
以及该不该动这个权重。

首轮（2026-08-31，368 个交易日 / 2025-01-02..2026-08-28 全市场）结论：
**保留 dry_q=0.20，不动权重。** 四格（H∈{5,10} × topN∈{10,20}）配对差
+0.591~+1.173pct、t=+2.11~+3.12，全部高于成本 0.202%；但走前动态选权重
一格都过不了 t>=2，宽网格下选中分布散在 0.4/0.8/1.2/2.0 之间——那是平坦
目标被拟合到噪声的特征。详见 core/ranker_weight_eval.py 模块头。

用法::

    python scripts/evaluate_ranker_weight.py --horizon 10
    python scripts/evaluate_ranker_weight.py --horizon 5 --no-notify
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

from core.ranker_weight_eval import (
    DRY_BANDS,
    MIN_AMOUNT_RAW,
    MIN_BAND_SIZE,
    MIN_GROUP,
    PROD_DRY_WEIGHT,
    PROD_Q3_WEIGHT,
    PROD_Q5_WEIGHT,
    PROD_Q20_WEIGHT,
    RANDOM_SEEDS,
    ROUND_TRIP_COST_PCT,
    TOP_N_GRID,
    WEIGHT_GRID,
    RankerReport,
    band_of,
    extension_penalty,
    matched_spread,
    render,
    summarize_ablation,
    summarize_band,
    summarize_weight,
    walk_forward_weight,
)

# 预热由 rolling 的 min_periods + dropna 自然完成：ret20 需 21 根、vol_ma20 需 20 根、
# min_vol_ratio_5d 再 +4，故 --start 应比目标区间提前约 25 个交易日。
FIELDS = "ts_code,trade_date,open,close,vol,amount"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="排序权重体检（dry_q）")
    parser.add_argument("--horizon", type=int, default=10, help="前瞻交易日数")
    parser.add_argument("--start", default="2025-01-01", help="行情起始（需留足 25 日预热）")
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
        try:
            frame = pro.daily(trade_date=day.replace("-", ""), fields=FIELDS)
            if frame is not None and not frame.empty:
                frames.append(frame)
        except Exception as exc:  # noqa: BLE001 - 单日失败不应中断整体检验
            print(f"[rankw] {day} 取数失败: {str(exc)[:60]}")
    if not frames:
        raise SystemExit("未取到行情")
    market = pd.concat(frames, ignore_index=True)
    market["trade_date"] = pd.to_numeric(market["trade_date"], errors="coerce").astype("Int64")
    return market.dropna(subset=["trade_date"]).sort_values(["ts_code", "trade_date"])


def build_features(market: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """特征只用 T 日及之前的 bar；买卖点为 T+1 开盘 / T+1+H 收盘。

    与 core/candidate_ranker.py 同式：vol_ratio = vol / vol.rolling(20).mean()，
    min_vol_ratio_5d 取尾 5 根的最小值。
    """
    df = market.copy()
    for col in ("open", "close", "vol", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    g = df.groupby("ts_code", sort=False)
    for name, lb in (("ret20", 20), ("ret5", 5), ("ret3", 3)):
        prev = g["close"].shift(lb)
        df[name] = (df["close"] / prev.where(prev > 0) - 1.0) * 100.0
    vol_ma20 = g["vol"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["vol_ratio"] = df["vol"] / vol_ma20.replace(0.0, np.nan)
    df["min_vol_ratio_5d"] = df.groupby("ts_code", sort=False)["vol_ratio"].transform(
        lambda s: s.rolling(5, min_periods=5).min()
    )
    # tushare amount 单位为千元，MIN_AMOUNT_RAW 已按此口径取值
    df["amt_ma20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    buy = g["open"].shift(-1)
    sell = g["close"].shift(-(1 + int(horizon)))
    df["fwd"] = (sell / buy.where(buy > 0) - 1.0) * 100.0 - ROUND_TRIP_COST_PCT
    keep = ["ts_code", "trade_date", "ret20", "ret5", "ret3", "min_vol_ratio_5d", "amt_ma20", "fwd"]
    out = df[keep].dropna()
    return out[out["amt_ma20"] >= MIN_AMOUNT_RAW].reset_index(drop=True)


def _score_day(day: pd.DataFrame) -> pd.DataFrame:
    """当日域内分位 + 动量臂（含 extension_penalty，与生产同式）。"""
    d = day.copy()
    d["q20"] = d["ret20"].rank(pct=True, method="average")
    d["q5"] = d["ret5"].rank(pct=True, method="average")
    d["q3"] = d["ret3"].rank(pct=True, method="average")
    # 缩量为好：min_vol_ratio_5d 越小 dry_q 越高
    d["dry_q"] = (-d["min_vol_ratio_5d"]).rank(pct=True, method="average")
    d["ext"] = [extension_penalty(r20, r5) for r20, r5 in zip(d["ret20"], d["ret5"], strict=True)]
    d["mom"] = PROD_Q20_WEIGHT * d["q20"] + PROD_Q5_WEIGHT * d["q5"] + PROD_Q3_WEIGHT * d["q3"] - d["ext"]
    return d


def _collect_bands(d: pd.DataFrame, domain: float, sink: dict[str, list[dict[str, float]]]) -> None:
    d = d.assign(band=[band_of(v) for v in d["dry_q"]])
    for label, group in d.dropna(subset=["band"]).groupby("band"):
        if len(group) >= MIN_BAND_SIZE:
            sink[str(label)].append({"inside": float(group["fwd"].mean()), "domain": domain, "size": float(len(group))})


def _collect_ablation(
    d: pd.DataFrame,
    date: int,
    ablation: dict[int, list[dict[str, float]]],
    rand: dict[int, dict[int, list[float]]],
) -> None:
    """留 vs 去 dry_q 的同日配对，外加同权重随机臂作无信息基准。"""
    keep_score = d["mom"] + PROD_DRY_WEIGHT * d["dry_q"]
    rng_cols: dict[int, pd.Series] = {}
    for seed in RANDOM_SEEDS:
        # 每日独立种子：保证跨日不相关，且分布与 dry_q 一致（均匀分位）
        rng = np.random.default_rng(int(date) * 1000 + seed)
        rq = pd.Series(rng.random(len(d)), index=d.index).rank(pct=True, method="average")
        rng_cols[seed] = d["mom"] + PROD_DRY_WEIGHT * rq
    for top_n in TOP_N_GRID:
        if len(d) < top_n * 3:
            continue
        keep_idx = keep_score.nlargest(top_n).index
        drop_idx = d["mom"].nlargest(top_n).index
        shared = len(set(keep_idx) & set(drop_idx)) / float(top_n)
        ablation[top_n].append(
            {
                "date": float(date),
                "keep": float(d.loc[keep_idx, "fwd"].mean()),
                "drop": float(d.loc[drop_idx, "fwd"].mean()),
                "overlap": shared,
            }
        )
        base = float(d.loc[drop_idx, "fwd"].mean())
        for seed, score in rng_cols.items():
            picked = float(d.loc[score.nlargest(top_n).index, "fwd"].mean())
            rand[top_n][seed].append(picked - base)


def _collect_weights(
    d: pd.DataFrame,
    domain: float,
    weights: dict[int, dict[float, list[dict[str, float]]]],
) -> None:
    for top_n, per_weight in weights.items():
        if len(d) < top_n * 3:
            continue
        for weight in WEIGHT_GRID:
            score = d["mom"] + weight * d["dry_q"]
            picked = float(d.loc[score.nlargest(top_n).index, "fwd"].mean())
            per_weight[weight].append({"inside": picked, "domain": domain})


def _collect_spread(d: pd.DataFrame, date: int, sink: list[dict[str, float]]) -> None:
    """固定 ret20 五档后的最干减最湿。见模块头「发现二」：接近零不代表无用。"""
    bucket = pd.qcut(d["ret20"].rank(method="first"), 5, labels=False, duplicates="drop")
    diffs = []
    for _, group in d.assign(b=bucket).dropna(subset=["b"]).groupby("b"):
        if len(group) < MIN_BAND_SIZE * 4:
            continue
        cut = max(len(group) // 5, MIN_BAND_SIZE)
        ordered = group.sort_values("dry_q")
        diffs.append(float(ordered["fwd"].tail(cut).mean() - ordered["fwd"].head(cut).mean()))
    if diffs:
        sink.append({"date": float(date), "spread": float(np.mean(diffs))})


def build_report(feats: pd.DataFrame, horizon: int) -> RankerReport:
    band_daily: dict[str, list[dict[str, float]]] = {label: [] for _, _, label in DRY_BANDS}
    ablation: dict[int, list[dict[str, float]]] = {n: [] for n in TOP_N_GRID}
    rand: dict[int, dict[int, list[float]]] = {n: {s: [] for s in RANDOM_SEEDS} for n in TOP_N_GRID}
    weights: dict[int, dict[float, list[dict[str, float]]]] = {n: {w: [] for w in WEIGHT_GRID} for n in TOP_N_GRID}
    spread_daily: list[dict[str, float]] = []
    wf_dates: list[int] = []
    wf_series: dict[float, list[float]] = {w: [] for w in WEIGHT_GRID}

    for date, day in feats.groupby("trade_date", sort=True):
        if len(day) < MIN_GROUP * 5:
            continue
        d = _score_day(day)
        domain = float(d["fwd"].mean())
        _collect_bands(d, domain, band_daily)
        _collect_ablation(d, int(date), ablation, rand)
        _collect_weights(d, domain, weights)
        _collect_spread(d, int(date), spread_daily)
        # 走前用 topN=10 这一格，与消融的主口径一致
        if len(d) >= TOP_N_GRID[0] * 3:
            wf_dates.append(int(date))
            for weight in WEIGHT_GRID:
                score = d["mom"] + weight * d["dry_q"]
                wf_series[weight].append(float(d.loc[score.nlargest(TOP_N_GRID[0]).index, "fwd"].mean()) - domain)

    report = RankerReport()
    report.bands = [summarize_band(label, band_daily[label]) for _, _, label in DRY_BANDS]
    report.ablation = [
        summarize_ablation(n, ablation[n], [float(np.mean(v)) for v in rand[n].values() if v]) for n in TOP_N_GRID
    ]
    report.weights = {n: [summarize_weight(w, weights[n][w]) for w in WEIGHT_GRID] for n in TOP_N_GRID}
    report.walk_forward = [walk_forward_weight(TOP_N_GRID[0], wf_dates, wf_series, horizon=horizon)]
    report.matched_spread = matched_spread(spread_daily)
    return report


def main() -> int:
    args = parse_args()
    horizon = max(int(args.horizon), 1)
    market = load_market(args.start)
    print(f"[rankw] 行情 {len(market):,} 行 / {market.ts_code.nunique()} 只")
    feats = build_features(market, horizon)
    if feats.empty:
        raise SystemExit("特征为空，检查区间是否留足预热")
    days = sorted(int(x) for x in feats["trade_date"].unique())
    print(f"[rankw] 域内 {len(feats):,} 行 / {len(days)} 个交易日 {days[0]}..{days[-1]}")
    report = build_report(feats, horizon)
    payload = report.as_dict()
    payload["horizon_days"] = horizon
    payload["window"] = {"start": days[0], "end": days[-1], "days": len(days)}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"ranker_weight_h{horizon}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = render(report, horizon=horizon, start=days[0], end=days[-1])
    (out_dir / f"ranker_weight_h{horizon}.md").write_text(text, encoding="utf-8")
    print(text)
    if not args.no_notify:
        _notify(text, horizon)
    return 0


def _notify(markdown: str, horizon: int) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[rankw] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    from utils.feishu import send_feishu_notification

    title = f"排序权重体检｜dry_q｜T+{horizon}｜{pd.Timestamp.now().strftime('%Y-%m-%d')}"
    print("[rankw] feishu sent" if send_feishu_notification(webhook, title, markdown) else "[rankw] feishu failed")


if __name__ == "__main__":
    raise SystemExit(main())
