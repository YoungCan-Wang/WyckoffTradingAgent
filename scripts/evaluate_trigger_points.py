"""触发分值体检（路径 B）：跑数、落盘、推飞书。

回答一个问题：``_trigger_score`` 那六个硬编码分值（``core/ai_candidate_allocation.py:559-582``，
12~50 分）排得对不对。这是**路径 B**，量级是 ``watch_score`` 里 ``trigger_q`` 的
十几倍，见 core/trigger_points_eval.py 模块头。

**复用 trigger_q 那轮的触发面板**，不自己重放（重放全市场约 90 分钟）。面板由::

    python scripts/evaluate_trigger_weight.py --gen-panel --start 2024-01-01

生成，落在 ``docs/evidence/.cache/trigger_panel.csv``。缓存面板决定统计窗口，不是
``--start``：面板要 210 个 bar 预热，若缓存是早先用较晚 ``--start`` 生成的，本脚本会
把行情裁到面板覆盖的日期，于是窗口静默停在旧区间。核对报告里的 ``window.days``。

用法::

    python scripts/evaluate_trigger_points.py --horizon 10
    python scripts/evaluate_trigger_points.py --horizon 5 --no-notify
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

from core.trigger_points_eval import (
    FLAT_POINTS,
    MIN_AMOUNT_RAW,
    PROD_POINTS,
    PROD_SOS_RESONANT,
    PROD_SOS_SINGLE,
    ROUND_TRIP_COST_PCT,
    TOP_N_GRID,
    TRIGGER_KINDS,
    PointsReport,
    SosResonanceStat,
    parse_kinds,
    path_b_score,
    permutation_tables,
    rank_correlation,
    render,
    summarize_arm,
    summarize_kind,
    summarize_permutation,
    summarize_quarters,
    topn_mean,
    tstat,
    walk_forward_narrow,
    walk_forward_table,
)

PANEL_CACHE = "docs/evidence/.cache"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="触发分值体检（_trigger_score 路径 B）")
    parser.add_argument("--horizon", type=int, default=10, help="前瞻交易日数")
    parser.add_argument("--out", default="docs/evidence", help="产物目录")
    parser.add_argument("--cache", default=PANEL_CACHE, help="行情/触发面板缓存目录")
    parser.add_argument("--no-notify", action="store_true", help="不推飞书")
    return parser.parse_args()


def load_market(cache: Path) -> pd.DataFrame:
    """复用 trigger_q 那轮落的行情缓存。缺了就让用户先跑那个脚本，不重复取数逻辑。"""
    path = cache / "market.csv"
    if not path.exists():
        raise SystemExit(f"缺少行情缓存 {path}，先跑 scripts/evaluate_trigger_weight.py --gen-panel")
    market = pd.read_csv(path, usecols=["ts_code", "trade_date", "open", "close", "amount"])
    market["trade_date"] = pd.to_numeric(market["trade_date"], errors="coerce").astype("Int64")
    return market.dropna(subset=["trade_date"]).sort_values(["ts_code", "trade_date"])


def build_features(market: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """前向收益与流动性域，口径与 trigger_q 那轮逐行一致。"""
    df = market.copy()
    for col in ("open", "close", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    g = df.groupby("ts_code", sort=False)
    # tushare amount 单位为千元，MIN_AMOUNT_RAW 已按此口径取值
    df["amt_ma20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    buy = g["open"].shift(-1)
    sell = g["close"].shift(-(1 + int(horizon)))
    df["fwd"] = (sell / buy.where(buy > 0) - 1.0) * 100.0 - ROUND_TRIP_COST_PCT
    out = df[["ts_code", "trade_date", "amt_ma20", "fwd"]].dropna()
    return out[out["amt_ma20"] >= MIN_AMOUNT_RAW].reset_index(drop=True)


def load_panel(cache: Path) -> pd.DataFrame:
    path = cache / "trigger_panel.csv"
    if not path.exists():
        raise SystemExit(f"缺少触发面板 {path}，先跑 scripts/evaluate_trigger_weight.py --gen-panel")
    panel = pd.read_csv(path, usecols=["ts_code", "trade_date", "n_hits", "kinds"])
    panel["trade_date"] = pd.to_numeric(panel["trade_date"], errors="coerce").astype("Int64")
    return panel.dropna(subset=["trade_date"])


def merge_panel(feats: pd.DataFrame, panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """内连接到触发面板：本轮只比较**已触发票之间**的排序，未触发的票不参与排序。

    但同日流动性域的基准 ``domain`` 必须在裁剪**前**算 —— 超额的分母是全域，
    不是触发子集，否则「触发本身是负超额」这件事会被抹掉。
    """
    covered = set(panel["trade_date"].dropna().astype(int).tolist())
    if covered:
        before = int(feats["trade_date"].nunique())
        feats = feats[feats["trade_date"].astype(int).isin(covered)].reset_index(drop=True)
        after = int(feats["trade_date"].nunique())
        if after < before:
            print(f"[trigp] 面板覆盖 {after} 日，行情有 {before} 日，按面板裁齐")
    domain = feats.groupby("trade_date")["fwd"].mean()
    merged = feats.merge(panel, on=["ts_code", "trade_date"], how="inner")
    merged["kinds"] = merged["kinds"].fillna("").astype(str)
    merged["n_hits"] = pd.to_numeric(merged["n_hits"], errors="coerce").fillna(0).astype(int)
    return merged[merged["kinds"] != ""].reset_index(drop=True), domain


def candidate_tables(kinds_excess: dict[str, float]) -> dict[str, dict[str, object]]:
    """走前挑表的候选集。每张表附 sos 的两档。

    候选只放**有先验理由**的四张，不做网格搜索 —— 网格搜出来的最优表几乎必然
    过拟合，走前那格会自己揭穿，但不如一开始就不给它机会。
    """
    prod = {"points": dict(PROD_POINTS), "sos_single": PROD_SOS_SINGLE, "sos_resonant": PROD_SOS_RESONANT}
    flat = {
        "points": dict.fromkeys(PROD_POINTS, FLAT_POINTS),
        "sos_single": FLAT_POINTS,
        "sos_resonant": FLAT_POINTS,
    }
    # 去共振：共振档降回单独档，其余不动。只动全表最大的那一次加分。
    no_res = {"points": dict(PROD_POINTS), "sos_single": PROD_SOS_SINGLE, "sos_resonant": PROD_SOS_SINGLE}
    # 按实测超额重排：把同一组数字重新分配给类别，超额越高给越高分。
    # 只重排、不改量级，与置换检验同构 —— 所以它是置换带的「最优端」。
    return {"prod": prod, "flat": flat, "no_res": no_res, "by_excess": _by_excess_table(kinds_excess)}


def _by_excess_table(kinds_excess: dict[str, float]) -> dict[str, object]:
    """把生产的分值集合按实测超额重新分配（超额高 -> 分值高）。

    这张表是**样本内最优的重排**，天然占便宜，所以它只该出现在走前那格里让
    「已结算历史」自己去挑；它在消融格里赢不算证据。
    """
    five = [k for k in PROD_POINTS if k in kinds_excess]
    if len(five) < len(PROD_POINTS):
        return {"points": dict(PROD_POINTS), "sos_single": PROD_SOS_SINGLE, "sos_resonant": PROD_SOS_RESONANT}
    vals = sorted(PROD_POINTS.values())
    order = sorted(five, key=lambda k: kinds_excess[k])
    points = dict(zip(order, vals, strict=True))
    sos_exc = kinds_excess.get("sos")
    # sos 的两档跟着它的相对位置走：超额最差就给最低档。
    single = min(vals) if sos_exc is not None and sos_exc <= min(kinds_excess.values()) else PROD_SOS_SINGLE
    return {"points": points, "sos_single": single, "sos_resonant": single}


def kind_excess(merged: pd.DataFrame, domain: pd.Series) -> tuple[list, dict[str, float]]:
    """单类型命中的各类超额。多类型样本不计入任何单类,避免重复计数。"""
    single = merged[merged["n_hits"] == 1]
    stats = []
    means: dict[str, float] = {}
    for kind in TRIGGER_KINDS:
        rows = single[single["kinds"] == kind]
        daily = []
        for day, grp in rows.groupby("trade_date", sort=False):
            base = float(domain.get(day, np.nan))
            if np.isfinite(base):
                daily.append(float(grp["fwd"].mean()) - base)
        stat = summarize_kind(kind, PROD_POINTS.get(kind, PROD_SOS_SINGLE), daily, rows=len(rows))
        stats.append(stat)
        if stat.excess is not None:
            means[kind] = float(stat.excess)
    return stats, means


def sos_resonance(merged: pd.DataFrame, domain: pd.Series) -> SosResonanceStat:
    """sos 单独 vs sos 共振。全表最大的一次加分,单独验。"""
    ks = merged["kinds"].map(parse_kinds)
    has_sos = ks.map(lambda s: "sos" in s)
    resonant = has_sos & ks.map(lambda s: len(s - {"sos"}) > 0)
    single = has_sos & ~resonant
    out = {}
    for label, mask in (("single", single), ("resonant", resonant)):
        rows = merged[mask]
        daily = []
        for day, grp in rows.groupby("trade_date", sort=False):
            base = float(domain.get(day, np.nan))
            if np.isfinite(base):
                daily.append(float(grp["fwd"].mean()) - base)
        out[label] = (
            float(np.mean(daily)) if daily else None,
            tstat(daily),
            len(rows),
        )
    return SosResonanceStat(
        single_excess=out["single"][0],
        single_t=out["single"][1],
        single_rows=out["single"][2],
        resonant_excess=out["resonant"][0],
        resonant_t=out["resonant"][1],
        resonant_rows=out["resonant"][2],
    )


class DayPanel:
    """把面板按日切成定长数组,供上千次重打分复用。

    置换检验要跑 ``N_PERMUTATIONS × len(TOP_N_GRID) × 交易日`` 次重打分,每次都
    ``groupby`` 会慢一个数量级。这里按「组合字符串 -> 分值」查表,分值表一换只重算
    几十个组合,不动上万行。
    """

    def __init__(self, merged: pd.DataFrame, domain: pd.Series) -> None:
        frame = merged.sort_values("trade_date", kind="stable").reset_index(drop=True)
        self.dates = [int(x) for x in frame["trade_date"].unique()]
        self.domain = np.array([float(domain.get(d, np.nan)) for d in self.dates], dtype=float)
        combos = pd.Categorical(frame["kinds"])
        self.combos = [str(c) for c in combos.categories]
        self.combo_kinds = [parse_kinds(c) for c in self.combos]
        codes = np.asarray(combos.codes, dtype=np.int64)
        fwd = frame["fwd"].to_numpy(dtype=float)
        bounds = np.searchsorted(frame["trade_date"].to_numpy(), self.dates, side="left")
        edges = list(bounds) + [len(frame)]
        self.day_codes = [codes[edges[i] : edges[i + 1]] for i in range(len(self.dates))]
        self.day_fwd = [fwd[edges[i] : edges[i + 1]] for i in range(len(self.dates))]

    def combo_scores(self, table: dict[str, object]) -> np.ndarray:
        points = table["points"]
        return np.array(
            [
                path_b_score(
                    ks,
                    points,  # type: ignore[arg-type]
                    sos_single=float(table["sos_single"]),  # type: ignore[arg-type]
                    sos_resonant=float(table["sos_resonant"]),  # type: ignore[arg-type]
                )
                for ks in self.combo_kinds
            ],
            dtype=float,
        )

    def excess_series(self, table: dict[str, object], top_n: int) -> list[float | None]:
        """按该表选 topN,逐日算相对同日流动性域的超额。"""
        lut = self.combo_scores(table)
        out: list[float | None] = []
        for idx, codes in enumerate(self.day_codes):
            base = self.domain[idx]
            picked = topn_mean(lut[codes], self.day_fwd[idx], top_n) if np.isfinite(base) else None
            out.append(None if picked is None else float(picked) - float(base))
        return out

    def tie_diagnostics(self, table: dict[str, object], top_n: int) -> tuple[int, int, int]:
        """唯一分数个数,以及 topN 边界并列桶的中位/最大宽度。

        这组数字是「为什么不能用 nlargest」的证据:边界桶多宽,index 顺序就替你选了
        多少只票。
        """
        lut = self.combo_scores(table)
        widths: list[int] = []
        uniq = 0
        for codes in self.day_codes:
            scores = lut[codes]
            if len(scores) < top_n:
                continue
            values, counts = np.unique(scores, return_counts=True)
            uniq = max(uniq, len(values))
            cum = np.cumsum(counts[::-1])
            pos = int(np.searchsorted(cum, top_n, side="left"))
            if pos < len(counts):
                widths.append(int(counts[::-1][pos]))
        if not widths:
            return uniq, 0, 0
        return uniq, int(np.median(widths)), int(max(widths))


def _clean(series: list[float | None]) -> list[float]:
    return [float(v) for v in series if v is not None and np.isfinite(float(v))]


def build_report(merged: pd.DataFrame, domain: pd.Series, horizon: int) -> PointsReport:
    panel = DayPanel(merged, domain)
    kinds, kind_means = kind_excess(merged, domain)
    tables = candidate_tables(kind_means)
    report = PointsReport(kinds=kinds, rank_corr=rank_correlation(kinds), sos=sos_resonance(merged, domain))

    perms = permutation_tables(PROD_POINTS)
    for top_n in TOP_N_GRID:
        series = {key: panel.excess_series(tbl, top_n) for key, tbl in tables.items()}
        prod = series["prod"]
        report.arms.append(summarize_arm("prod", top_n, _clean(prod)))
        for key in ("flat", "no_res", "by_excess"):
            report.arms.append(summarize_arm(key, top_n, series[key], prod=prod))
        # 置换只换对应关系,sos 两档跟着生产不动 —— 换了就不是「同一组数字」了
        perm_means = []
        for table in perms:
            arm = _clean(panel.excess_series({**tables["prod"], "points": table}, top_n))
            if arm:
                perm_means.append(float(np.mean(arm)))
        prod_clean = _clean(prod)
        report.permutations.append(
            summarize_permutation(top_n, float(np.mean(prod_clean)) if prod_clean else None, perm_means)
        )
        report.walk_forward.append(walk_forward_table(top_n, panel.dates, series, horizon=horizon))
        # 并列一格,不替换上面那格:两格问的是不同问题,见 walk_forward_narrow 的 docstring。
        report.walk_forward_narrow.append(
            walk_forward_narrow(top_n, panel.dates, prod, series["flat"], horizon=horizon)
        )
        if top_n == TOP_N_GRID[0]:
            uniq, med, mx = panel.tie_diagnostics(tables["prod"], top_n)
            report.unique_scores, report.tie_bucket_median, report.tie_bucket_max = uniq, med, mx
            pairs = [
                (d, float(f) - float(p))
                for d, f, p in zip(panel.dates, series["flat"], prod, strict=True)
                if f is not None and p is not None and np.isfinite(float(f)) and np.isfinite(float(p))
            ]
            report.quarters = summarize_quarters([d for d, _ in pairs], [v for _, v in pairs])
    return report


def main() -> int:
    args = parse_args()
    horizon = max(int(args.horizon), 1)
    cache = Path(args.cache)
    market = load_market(cache)
    print(f"[trigp] 行情 {len(market):,} 行 / {market.ts_code.nunique()} 只")
    feats = build_features(market, horizon)
    if feats.empty:
        raise SystemExit("特征为空，检查区间是否留足预热")
    merged, domain = merge_panel(feats, load_panel(cache))
    if merged.empty:
        raise SystemExit("面板与行情无交集，先重新生成 trigger_panel.csv")
    days = sorted(int(x) for x in merged["trade_date"].unique())
    print(f"[trigp] 触发 {len(merged):,} 行 / {len(days)} 日 {days[0]}..{days[-1]}")
    report = build_report(merged, domain, horizon)
    payload = report.as_dict()
    payload["horizon_days"] = horizon
    payload["window"] = {"start": days[0], "end": days[-1], "days": len(days)}
    payload["triggered_rows"] = len(merged)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"trigger_points_h{horizon}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = render(report, horizon=horizon, start=days[0], end=days[-1])
    (out_dir / f"trigger_points_h{horizon}.md").write_text(text, encoding="utf-8")
    print(text)
    if not args.no_notify:
        _notify(text, horizon)
    return 0


def _notify(markdown: str, horizon: int) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[trigp] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    from utils.feishu import send_feishu_notification

    title = f"触发分值体检｜_trigger_score｜T+{horizon}｜{pd.Timestamp.now().strftime('%Y-%m-%d')}"
    print("[trigp] feishu sent" if send_feishu_notification(webhook, title, markdown) else "[trigp] feishu failed")


if __name__ == "__main__":
    raise SystemExit(main())
