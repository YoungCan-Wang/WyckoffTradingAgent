"""排序权重体检（trigger_q）：重放触发、跑数、落盘、推飞书。

回答一个问题：``watch_score`` 里的 ``trigger_q``（生产权重 0.30，单项最大）
值不值这个分量。

与 dry_q 那轮不同，这一轮方向指向负：触发本身是负超额、幅度排序不带信息、
六种触发无一为正。但幅度不稳（192 日窗口 -1.461，补到 423 日腰斩到 -0.735），
走前挑权重 t<2 没过线，故**本轮不改生产参数**。详见
core/trigger_weight_eval.py 模块头与 docs/evidence/trigger_weight_h*.md。

两段式：``--gen-panel`` 先逐票逐日重放生产 ``layer4_triggers`` 的六个检测器
（多进程，全市场约 90 分钟），落一份触发面板缓存；之后的统计直接读缓存。

**缓存面板决定了统计窗口，不是 ``--start``。** 面板需 210 个 bar 预热，若缓存是
早先用较晚 ``--start`` 生成的，``merge_panel`` 会把行情裁到面板覆盖的日期
（不裁的话幅度按覆盖比例线性稀释），于是窗口静默停在旧区间。首轮就这么只跑了
192 日、把幅度读大了一倍。换窗口时删掉 ``docs/evidence/.cache/trigger_panel.csv``
重新 ``--gen-panel``，并核对报告里的 ``window.days``。

用法::

    python scripts/evaluate_trigger_weight.py --gen-panel --start 2024-01-01
    python scripts/evaluate_trigger_weight.py --horizon 10
    python scripts/evaluate_trigger_weight.py --horizon 5 --no-notify
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

from core.trigger_weight_eval import (
    L3_PROXY_SIZES,
    MIN_AMOUNT_RAW,
    MIN_DAYS,
    MIN_HALF,
    MIN_REPLAY_BARS,
    MIN_TRIGGERED,
    PROD_DRY_WEIGHT,
    PROD_Q3_WEIGHT,
    PROD_Q5_WEIGHT,
    PROD_Q20_WEIGHT,
    PROD_TRIGGER_WEIGHT,
    RANDOM_SEEDS,
    ROUND_TRIP_COST_PCT,
    TOP_N_GRID,
    TRIGGER_KINDS,
    WEIGHT_GRID,
    TriggerReport,
    extension_penalty,
    production_detectors,
    render,
    replay_entry_bias_limit,
    summarize_ablation,
    summarize_binary,
    summarize_kind,
    summarize_magnitude,
    summarize_pool,
    summarize_weight,
    walk_forward_weight,
)

# 检测器要看 200 日 MA，外加缓冲；生产 df_map 同样是长历史。定义在 core 侧，
# 与 production_detectors() 的回看长度同源。
MIN_BARS = MIN_REPLAY_BARS
FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg"
PANEL_CACHE = "docs/evidence/.cache"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="排序权重体检（trigger_q）")
    parser.add_argument("--horizon", type=int, default=10, help="前瞻交易日数")
    parser.add_argument("--start", default="2024-01-01", help="行情起始（需留足 210 日预热）")
    parser.add_argument("--out", default="docs/evidence", help="产物目录")
    parser.add_argument("--cache", default=PANEL_CACHE, help="行情/触发面板缓存目录")
    parser.add_argument("--gen-panel", action="store_true", help="重放触发面板（慢，约 90 分钟）")
    parser.add_argument("--workers", type=int, default=0, help="重放进程数，0 = CPU-2")
    parser.add_argument("--no-notify", action="store_true", help="不推飞书")
    return parser.parse_args()


def load_market(start: str, cache: Path) -> pd.DataFrame:
    """全市场日线，带本地缓存。按交易日批量取，逐只取会慢一个数量级。"""
    path = cache / "market.csv"
    if path.exists():
        market = pd.read_csv(path)
        print(f"[trigw] 复用缓存行情 {len(market):,} 行")
    else:
        market = _fetch_market(start)
        cache.mkdir(parents=True, exist_ok=True)
        market.to_csv(path, index=False)
    market["trade_date"] = pd.to_numeric(market["trade_date"], errors="coerce").astype("Int64")
    return market.dropna(subset=["trade_date"]).sort_values(["ts_code", "trade_date"])


def _fetch_market(start: str) -> pd.DataFrame:
    from integrations.fetch_a_share_csv import cached_trade_dates
    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise SystemExit("需要 TUSHARE_TOKEN")
    end = pd.Timestamp.now().strftime("%Y-%m-%d")
    days = [str(day) for day in cached_trade_dates() if start <= str(day) <= end]
    frames = []
    for day in days:
        for _ in range(3):  # 单日重试，整轮取数近 700 次调用，偶发失败不该丢整天
            try:
                frame = pro.daily(trade_date=day.replace("-", ""), fields=FIELDS)
                if frame is not None and not frame.empty:
                    frames.append(frame)
                break
            except Exception as exc:  # noqa: BLE001 - 单日失败不应中断整体检验
                last = str(exc)[:60]
        else:
            print(f"[trigw] {day} 取数失败: {last}")
    if not frames:
        raise SystemExit("未取到行情")
    return pd.concat(frames, ignore_index=True)


def build_features(market: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """特征只用 T 日及之前的 bar；买卖点为 T+1 开盘 / T+1+H 收盘。"""
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


def load_panel(cache: Path) -> pd.DataFrame:
    """触发面板。列：ts_code / trade_date / trigger_score / n_hits / kinds。"""
    path = cache / "trigger_panel.csv"
    if not path.exists():
        raise SystemExit(f"缺少触发面板 {path}，先跑 --gen-panel")
    panel = pd.read_csv(path)
    panel["trade_date"] = pd.to_numeric(panel["trade_date"], errors="coerce").astype("Int64")
    panel["trigger_score"] = pd.to_numeric(panel["trigger_score"], errors="coerce")
    return panel.dropna(subset=["trade_date", "trigger_score"])


def merge_panel(feats: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    """左连接：未命中触发的票 trigger_score 填 0.0，并**裁到面板覆盖的日期**。

    填 0 而非 NaN 是刻意与生产对齐——candidate_score_value(None) 返回 0.0，
    所以生产里「没触发」和「触发但分数为 0」在 trigger_q 上不可区分。

    裁日期是必须的，否则会静默稀释结论：行情缓存的区间通常宽于面板（面板要 210
    个 bar 预热，且可能是旧区间留下的），面板外的日子全票填 0 -> trigger_q 退化成
    常数 -> keep 臂与 drop 臂选出同一批票、配对差恒为 0。这些零差值不会让 t 值失真
    方向，但会按覆盖比例把幅度线性拉向零：192/610 的覆盖率把 -1.46 读成 -0.43。
    """
    covered = set(panel["trade_date"].dropna().astype(int).tolist())
    if covered:
        before = int(feats["trade_date"].nunique())
        feats = feats[feats["trade_date"].astype(int).isin(covered)].reset_index(drop=True)
        after = int(feats["trade_date"].nunique())
        if after < before:
            print(f"[trigw] 面板覆盖 {after} 日，行情有 {before} 日，按面板裁齐（否则幅度被稀释）")
    merged = feats.merge(panel, on=["ts_code", "trade_date"], how="left")
    merged["trigger_score"] = merged["trigger_score"].fillna(0.0)
    merged["n_hits"] = merged["n_hits"].fillna(0).astype(int)
    merged["kinds"] = merged["kinds"].fillna("")
    return merged


def build_replay_payload(market: pd.DataFrame) -> dict[str, object]:
    """把全市场行情摊成共享给子进程的列数组。

    单独成函数是为了让测试能不起进程池就走完整条重放路径：
    build_replay_payload -> _init_worker -> _replay_symbol。之前这段内联在
    gen_panel 里，测试只能另手搓一个帧,于是漏列这类事测不出来 —— 首轮定时轮
    就是死在这:帧里没 pct_chg,_detect_evr 与 _detect_sos 在池子里抛 KeyError。
    """
    df = market.copy()
    if "pct_chg" not in df.columns:
        # 缓存行情可能是早于 FIELDS 的旧文件。不用 close.pct_change() 兜底：
        # tushare daily 给的是不复权价，除权日 close 跳空，推出来的涨跌幅在那些
        # 日子上是假的大值，正好落在 _detect_evr / _detect_sos 的判据上。
        raise SystemExit(f"缓存行情缺 pct_chg 列，删掉 {PANEL_CACHE}/market.csv 重新取数")
    for col in ("open", "high", "low", "close", "vol"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "vol"])
    # pct_chg 不进 dropna：每只票首个 bar 无前收,天然是 NaN,检测器逐日自己判
    # （_detect_evr 在 day_pct 处、_detect_sos 同式）。丢行会把首 bar 连带删掉。
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
    codes, cidx = np.unique(df["ts_code"].to_numpy(), return_inverse=True)
    return {
        "codes": codes,
        "cidx": cidx,
        "dates": df["trade_date"].to_numpy(dtype=np.int64),
        "open": df["open"].to_numpy(dtype=np.float64),
        "high": df["high"].to_numpy(dtype=np.float64),
        "low": df["low"].to_numpy(dtype=np.float64),
        "close": df["close"].to_numpy(dtype=np.float64),
        "volume": df["vol"].to_numpy(dtype=np.float64),
        "pct_chg": df["pct_chg"].to_numpy(dtype=np.float64),
        "turnover": _replay_turnover(df),
    }


def _replay_turnover(df: pd.DataFrame) -> np.ndarray:
    """换手率(%)，与生产 workflows/funnel_data.py::_attach_turnover 同式同源。

    生产在装配数据时就补了这一列，重放不补的话 _evr_turnover_ok 走"缺列即通过"
    分支，量到的是一个比生产更松的 EVR：实测重放 EVR 触发日有 21% 落在
    evr_min_turnover=1.5 以下，即多算约 27%。而模块头说重放触发率是"下界"
    （漏了 L2 通道放宽），方向正相反 —— 两个偏差同时在，面板的 EVR 触发率
    哪个方向都不构成界。

    单位链错 100 倍会凭空造出或抹掉整个效应，逐段核过：生产
    data_source_tushare 把 vol 乘 100 后存进 volume（股），这里的 vol 是
    tushare 原始值（手），流通股本缓存已是股，故 vol*100/float_share*100。
    """
    from integrations.market_metadata import fetch_float_share_map

    float_share_map = fetch_float_share_map()
    if not float_share_map:
        # 不学生产的 warning-then-continue：生产宁可少一道闸也要出票，而一份
        # 静默量着更松检测器的评估产物比没有产物更糟 —— 拿它的幅度推断生产,
        # 错的方向还不知道。
        raise SystemExit("流通股本映射为空，重放会量到比生产更松的 EVR；先修数据源再跑")
    symbols = df["ts_code"].astype(str).str.split(".").str[0]
    # 缺流通股本的填 NaN 不填 0：_evr_turnover_ok 对 NaN 也是放行，与生产对齐；
    # 填 0 会把方向反过来变成整只票全拒。
    float_share = symbols.map(float_share_map).astype(float)
    float_share = float_share.where(float_share > 0)
    turnover = df["vol"].to_numpy(dtype=np.float64) * 100.0 / float_share.to_numpy(dtype=np.float64) * 100.0
    covered = int(np.isfinite(turnover).sum())
    print(f"[trigw] 换手率折算覆盖: {covered:,}/{len(turnover):,} ({covered / max(len(turnover), 1):.1%})")
    return turnover


_PANEL: dict[str, object] = {}


def _init_worker(payload: dict[str, object]) -> None:
    from core.wyckoff_engine import FunnelConfig

    _PANEL.update(payload)
    _PANEL["cfg"] = FunnelConfig()
    _PANEL["detectors"] = production_detectors()


def _replay_symbol(ci: int) -> list[tuple[str, int, float, int, str]]:
    """单只票逐日重放。每个截面只喂 T 日及之前的 bar，不含未来信息。"""
    codes = _PANEL["codes"]  # type: ignore[index]
    cidx = _PANEL["cidx"]  # type: ignore[index]
    cfg = _PANEL["cfg"]
    detectors = _PANEL["detectors"]
    code = str(codes[ci])
    mask = cidx == ci
    if int(mask.sum()) < MIN_BARS:
        return []
    # 这些列名要和生产 df_map 对齐:六个检测器里 _detect_evr 与 _detect_sos 都读
    # pct_chg,缺了会在池子里抛 KeyError 而整轮重放退出（面板生成不了,依赖它的
    # Trigger Points Eval 也跟着挂）。turnover 缺的表现相反 —— 不报错,
    # _evr_turnover_ok 静默放行,量到一个比生产松 27% 的 EVR。加列前先看
    # production_detectors() 读了什么,也看生产 df_map 装配时补了什么。
    frame = pd.DataFrame(
        {
            "date": _PANEL["dates"][mask],  # type: ignore[index]
            "open": _PANEL["open"][mask],  # type: ignore[index]
            "high": _PANEL["high"][mask],  # type: ignore[index]
            "low": _PANEL["low"][mask],  # type: ignore[index]
            "close": _PANEL["close"][mask],  # type: ignore[index]
            "volume": _PANEL["volume"][mask],  # type: ignore[index]
            "pct_chg": _PANEL["pct_chg"][mask],  # type: ignore[index]
            "turnover": _PANEL["turnover"][mask],  # type: ignore[index]
        }
    ).sort_values("date", ignore_index=True)
    out: list[tuple[str, int, float, int, str]] = []
    for end in range(MIN_BARS, len(frame)):
        sub = frame.iloc[: end + 1]
        limit = replay_entry_bias_limit(code, sub, cfg)
        best: float | None = None
        kinds: list[str] = []
        for key, fn in detectors:  # type: ignore[misc]
            score = fn(sub, cfg, max_bias_200=limit, code=code)
            if score is None:
                continue
            kinds.append(key)
            best = float(score) if best is None else max(best, float(score))
        if best is not None:
            out.append((code, int(sub["date"].iloc[-1]), best, len(kinds), "|".join(kinds)))
    return out


def gen_panel(market: pd.DataFrame, cache: Path, workers: int) -> None:
    """多进程重放触发面板。全市场约 90 分钟，结果落缓存供后续统计复用。"""
    from multiprocessing import Pool, cpu_count

    payload = build_replay_payload(market)
    codes = payload["codes"]
    n_workers = workers if workers > 0 else max(cpu_count() - 2, 2)
    print(f"[trigw] 重放 {len(codes)} 只 / {n_workers} 进程")
    rows: list[tuple[str, int, float, int, str]] = []
    with Pool(n_workers, initializer=_init_worker, initargs=(payload,)) as pool:
        for i, chunk in enumerate(pool.imap_unordered(_replay_symbol, range(len(codes)), chunksize=8), start=1):
            rows.extend(chunk)
            if i % 400 == 0:
                print(f"  {i}/{len(codes)} rows={len(rows)}", flush=True)
    panel = pd.DataFrame(rows, columns=["ts_code", "trade_date", "trigger_score", "n_hits", "kinds"])
    cache.mkdir(parents=True, exist_ok=True)
    panel.to_csv(cache / "trigger_panel.csv", index=False)
    print(f"[trigw] 触发面板 {len(panel):,} 行 / {panel.ts_code.nunique()} 只")


def _score_day(day: pd.DataFrame) -> pd.DataFrame:
    """当日域内分位 + 动量基分（含 extension_penalty 与 dry_q，与生产同式）。

    基分带上 dry_q 是必须的：上一轮已证实 dry_q 有正贡献，基准臂不带它就比
    生产天真，会把 trigger_q 的减益算大。
    """
    d = day.copy()
    d["q20"] = d["ret20"].rank(pct=True, method="average")
    d["q5"] = d["ret5"].rank(pct=True, method="average")
    d["q3"] = d["ret3"].rank(pct=True, method="average")
    d["dry_q"] = (-d["min_vol_ratio_5d"]).rank(pct=True, method="average")
    d["ext"] = [extension_penalty(r20, r5) for r20, r5 in zip(d["ret20"], d["ret5"], strict=True)]
    d["base"] = (
        PROD_Q20_WEIGHT * d["q20"]
        + PROD_Q5_WEIGHT * d["q5"]
        + PROD_Q3_WEIGHT * d["q3"]
        + PROD_DRY_WEIGHT * d["dry_q"]
        - d["ext"]
    )
    d["trigger_q"] = d["trigger_score"].rank(pct=True, method="average")
    d["flag"] = (d["trigger_score"] > 0).astype(float)
    return d


def _collect_questions(
    d: pd.DataFrame,
    date: int,
    domain: float,
    binary: list[dict[str, float]],
    magnitude: list[dict[str, float]],
    kinds: dict[str, list[dict[str, float]]],
) -> None:
    """Q1 二元 / Q2 幅度 / Q3 类型，三问共用一次当日切分。"""
    trg = d[d["trigger_score"] > 0]
    if len(trg) >= MIN_TRIGGERED:
        binary.append({"inside": float(trg["fwd"].mean()), "domain": domain, "size": float(len(trg))})
        ordered = trg.sort_values("trigger_score")
        half = len(ordered) // 2
        if half >= MIN_HALF:
            row = {
                "date": float(date),
                "spread": float(ordered["fwd"].tail(half).mean() - ordered["fwd"].head(half).mean()),
            }
            # 环境无 scipy，Spearman 手算成「秩上的 Pearson」
            ic = trg["trigger_score"].rank(method="average").corr(trg["fwd"].rank(method="average"))
            if ic is not None and np.isfinite(ic):
                row["ic"] = float(ic)
            magnitude.append(row)
    # 只取单一类型命中，避免多类型样本被重复计入不同 kind
    single = trg[trg["n_hits"] == 1]
    for kind, group in single.groupby("kinds"):
        if str(kind) in kinds and len(group) >= MIN_HALF:
            kinds[str(kind)].append({"inside": float(group["fwd"].mean()), "domain": domain, "size": float(len(group))})


def _collect_ablation(
    d: pd.DataFrame,
    date: int,
    ablation: dict[int, list[dict[str, float]]],
    rand: dict[int, dict[int, list[float]]],
) -> None:
    """四臂同日配对：keep / drop / binary / rand。"""
    keep_score = d["base"] + PROD_TRIGGER_WEIGHT * d["trigger_q"]
    bin_score = d["base"] + PROD_TRIGGER_WEIGHT * d["flag"]
    rng_cols: dict[int, pd.Series] = {}
    for seed in RANDOM_SEEDS:
        # 每日独立种子：保证跨日不相关，且分布与 trigger_q 一致（均匀分位）
        rng = np.random.default_rng(int(date) * 1000 + seed)
        rq = pd.Series(rng.random(len(d)), index=d.index).rank(pct=True, method="average")
        rng_cols[seed] = d["base"] + PROD_TRIGGER_WEIGHT * rq
    for top_n in TOP_N_GRID:
        if len(d) < top_n * 3:
            continue
        keep_idx = keep_score.nlargest(top_n).index
        drop_idx = d["base"].nlargest(top_n).index
        ablation[top_n].append(
            {
                "date": float(date),
                "keep": float(d.loc[keep_idx, "fwd"].mean()),
                "drop": float(d.loc[drop_idx, "fwd"].mean()),
                "binary": float(d.loc[bin_score.nlargest(top_n).index, "fwd"].mean()),
                "overlap": len(set(keep_idx) & set(drop_idx)) / float(top_n),
            }
        )
        base = float(d.loc[drop_idx, "fwd"].mean())
        for seed, score in rng_cols.items():
            rand[top_n][seed].append(float(d.loc[score.nlargest(top_n).index, "fwd"].mean()) - base)


def _collect_weights(
    d: pd.DataFrame,
    domain: float,
    weights: dict[int, dict[float, list[dict[str, float]]]],
) -> None:
    for top_n, per_weight in weights.items():
        if len(d) < top_n * 3:
            continue
        for weight in WEIGHT_GRID:
            score = d["base"] + weight * d["trigger_q"]
            picked = float(d.loc[score.nlargest(top_n).index, "fwd"].mean())
            per_weight[weight].append({"inside": picked, "domain": domain})


def _collect_pools(d: pd.DataFrame, pools: dict[tuple[int, int], list[dict[str, float]]]) -> None:
    """L3 代理：域内按动量基分取前 K 名，池内**重算** trigger_q 后再比。

    重算是关键——生产 rank_l3_candidates 在 L3 存活集上算分位，不是在流动性域上。
    """
    for (pool_size, top_n), sink in pools.items():
        if len(d) < pool_size:
            continue
        pool = d.nlargest(pool_size, "base").copy()
        if pool["trigger_score"].nunique() <= 1:
            continue
        pool["trigger_q"] = pool["trigger_score"].rank(pct=True, method="average")
        keep_score = pool["base"] + PROD_TRIGGER_WEIGHT * pool["trigger_q"]
        sink.append(
            {
                "keep": float(pool.loc[keep_score.nlargest(top_n).index, "fwd"].mean()),
                "drop": float(pool.loc[pool["base"].nlargest(top_n).index, "fwd"].mean()),
                "rate": float((pool["trigger_score"] > 0).mean()),
            }
        )


def build_report(merged: pd.DataFrame, horizon: int) -> TriggerReport:
    binary: list[dict[str, float]] = []
    magnitude: list[dict[str, float]] = []
    kind_daily: dict[str, list[dict[str, float]]] = {k: [] for k in TRIGGER_KINDS}
    ablation: dict[int, list[dict[str, float]]] = {n: [] for n in TOP_N_GRID}
    rand: dict[int, dict[int, list[float]]] = {n: {s: [] for s in RANDOM_SEEDS} for n in TOP_N_GRID}
    weights: dict[int, dict[float, list[dict[str, float]]]] = {n: {w: [] for w in WEIGHT_GRID} for n in TOP_N_GRID}
    pools: dict[tuple[int, int], list[dict[str, float]]] = {
        (size, n): [] for size in L3_PROXY_SIZES for n in TOP_N_GRID
    }
    wf_dates: list[int] = []
    wf_series: dict[int, dict[float, list[float]]] = {n: {w: [] for w in WEIGHT_GRID} for n in TOP_N_GRID}

    for date, day in merged.groupby("trade_date", sort=True):
        if len(day) < MIN_DAYS * 5:
            continue
        d = _score_day(day)
        domain = float(d["fwd"].mean())
        _collect_questions(d, int(date), domain, binary, magnitude, kind_daily)
        _collect_ablation(d, int(date), ablation, rand)
        _collect_weights(d, domain, weights)
        _collect_pools(d, pools)
        wf_dates.append(int(date))
        for top_n in TOP_N_GRID:
            for weight in WEIGHT_GRID:
                score = d["base"] + weight * d["trigger_q"]
                inside = float(d.loc[score.nlargest(top_n).index, "fwd"].mean())
                wf_series[top_n][weight].append(inside - domain)

    report = TriggerReport()
    report.binary = summarize_binary(binary)
    report.magnitude = summarize_magnitude(magnitude)
    report.kinds = [summarize_kind(k, kind_daily[k]) for k in TRIGGER_KINDS]
    report.ablation = [
        summarize_ablation(n, ablation[n], [float(np.mean(v)) for v in rand[n].values() if v]) for n in TOP_N_GRID
    ]
    report.weights = {n: [summarize_weight(w, weights[n][w]) for w in WEIGHT_GRID] for n in TOP_N_GRID}
    report.pools = [summarize_pool(size, n, rows) for (size, n), rows in sorted(pools.items())]
    report.walk_forward = [walk_forward_weight(n, wf_dates, wf_series[n], horizon=horizon) for n in TOP_N_GRID]
    report.kind_medians = _kind_medians(merged)
    hits = merged.loc[merged["n_hits"] > 0, "n_hits"].value_counts()
    report.hits_dist = {int(k): int(v) for k, v in hits.items()}
    return report


def _kind_medians(merged: pd.DataFrame) -> dict[str, float]:
    """单类型命中的 trigger_score 中位数，用于展示量纲不重叠。"""
    single = merged[(merged["n_hits"] == 1) & (merged["trigger_score"] > 0)]
    out: dict[str, float] = {}
    for kind, group in single.groupby("kinds"):
        if str(kind) in TRIGGER_KINDS and len(group) >= MIN_TRIGGERED:
            out[str(kind)] = float(group["trigger_score"].median())
    return out


def main() -> int:
    args = parse_args()
    horizon = max(int(args.horizon), 1)
    cache = Path(args.cache)
    market = load_market(args.start, cache)
    print(f"[trigw] 行情 {len(market):,} 行 / {market.ts_code.nunique()} 只")
    if args.gen_panel:
        gen_panel(market, cache, int(args.workers))
    feats = build_features(market, horizon)
    if feats.empty:
        raise SystemExit("特征为空，检查区间是否留足预热")
    merged = merge_panel(feats, load_panel(cache))
    days = sorted(int(x) for x in merged["trade_date"].unique())
    rate = float((merged["trigger_score"] > 0).mean())
    print(f"[trigw] 域内 {len(merged):,} 行 / {len(days)} 日 {days[0]}..{days[-1]}，触发率 {rate:.2%}")
    report = build_report(merged, horizon)
    payload = report.as_dict()
    payload["horizon_days"] = horizon
    payload["window"] = {"start": days[0], "end": days[-1], "days": len(days)}
    payload["domain_trigger_rate"] = round(rate, 4)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"trigger_weight_h{horizon}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = render(report, horizon=horizon, start=days[0], end=days[-1])
    (out_dir / f"trigger_weight_h{horizon}.md").write_text(text, encoding="utf-8")
    print(text)
    if not args.no_notify:
        _notify(text, horizon)
    return 0


def _notify(markdown: str, horizon: int) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[trigw] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    from utils.feishu import send_feishu_notification

    title = f"排序权重体检｜trigger_q｜T+{horizon}｜{pd.Timestamp.now().strftime('%Y-%m-%d')}"
    print("[trigw] feishu sent" if send_feishu_notification(webhook, title, markdown) else "[trigw] feishu failed")


if __name__ == "__main__":
    raise SystemExit(main())
