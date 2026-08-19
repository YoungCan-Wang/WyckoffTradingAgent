"""检验外部资金佐证与信号 health 是否有前瞻 alpha（可复算，随样本增长重跑）。

回答两个问题，两者都必须在**接线之前**先过：

1. `signal_health_daily` 的 HEALTHY 状态能否挑出前瞻更好的候选？
   这是 shadow promotion 晋级判据的前提。2026-08-14 首次跑数：HEALTHY 前瞻 T+5
   −12.74%，是四档最差；同日配对差 −2.82pct、CI [−5.22, −0.26]；health 历史统计与
   实际前瞻收益相关系数 −0.116。故晋级默认关闭。
2. 龙虎榜 / 融资融券 / 大宗交易 / 资金流等资金佐证，是否让候选的前瞻表现更好？
   首次跑数时 5,453 条 observation 里只有 3 条带 `source_context`（0.1%），无法判断；
   放宽取数上限后需重跑。

方法约束（与 scripts/ablate_trend_drawdown_gate.py 一致）：
- **只用事件日之前的 health**（`direction="backward"`、`allow_exact_matches=False`），
  否则会用到当日或未来才知道的健康度。
- **按交易日等权**，再做**同一交易日内配对**剥离市场水温——首次跑数时 HEALTHY 集中在
  7 月（该月整体 −8.07%），不配对会把月份效应当成信号。
- **随机负控制在每个交易日内打乱标签**，保留聚类结构后再比带宽。
- 同时报告**剔除最大信号**后的结果：首次跑数中 launchpad 占 HEALTHY 的 101/133，
  剔除后差值落回噪声，说明结论由单一信号驱动、尚未确证。

用法::

    python scripts/evaluate_capital_context_alpha.py --horizon 5 --out artifacts/evidence
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
import pandas as pd

from core.trade_friction import net_return_pct, round_trip_cost_pct

MIN_GROUP = 30
BOOTSTRAP_ROUNDS = 2000
CONTROL_SEEDS = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="资金佐证 / 信号 health 的前瞻 alpha 检验")
    parser.add_argument("--horizon", type=int, default=5, help="前瞻天数，对应 signal_outcomes.horizon_days")
    parser.add_argument("--out", default="artifacts/evidence", help="结果输出目录")
    return parser.parse_args()


def _fetch_all(client: Any, table: str, *, cap: int = 60_000) -> pd.DataFrame:
    rows: list[dict] = []
    offset = 0
    while offset < cap:
        page = client.table(table).select("*").range(offset, offset + 999).execute()
        if not page.data:
            break
        rows.extend(page.data)
        offset += 1000
    return pd.DataFrame(rows)


def _day_weighted(frame: pd.DataFrame, column: str = "return_pct") -> float | None:
    if frame.empty:
        return None
    return float(frame.groupby("d")[column].mean().mean())


def _paired_by_day(frame: pd.DataFrame, flag: str) -> pd.DataFrame:
    """只保留同日两组都有样本的交易日，返回逐日均值差。"""
    rows = []
    for day, group in frame.groupby("d"):
        left = group[group[flag] == 1]
        right = group[group[flag] == 0]
        if left.empty or right.empty:
            continue
        rows.append(
            {
                "d": day,
                "treated": left.return_pct.mean(),
                "control": right.return_pct.mean(),
                "n_treated": len(left),
                "n_control": len(right),
            }
        )
    return pd.DataFrame(rows)


def _paired_bootstrap_ci(diffs: list[float], rng: random.Random) -> tuple[float | None, float | None]:
    if len(diffs) < 3:
        return (None, None)
    means = []
    for _ in range(BOOTSTRAP_ROUNDS):
        sample = [diffs[rng.randrange(len(diffs))] for _ in range(len(diffs))]
        means.append(sum(sample) / len(sample))
    means.sort()
    return (round(means[int(0.025 * (len(means) - 1))], 4), round(means[int(0.975 * (len(means) - 1))], 4))


def _intraday_shuffle_band(frame: pd.DataFrame, flag: str) -> tuple[float | None, float | None]:
    """在每个交易日内打乱分组标签，保留聚类结构后得到噪声带宽。"""
    diffs = []
    for seed in range(CONTROL_SEEDS):
        rng = random.Random(seed)
        parts = []
        for _, group in frame.groupby("d"):
            block = group.copy()
            treated_n = int((block[flag] == 1).sum())
            index = list(block.index)
            rng.shuffle(index)
            picked = set(index[:treated_n])
            block["_fake"] = [1 if idx in picked else 0 for idx in block.index]
            parts.append(block)
        shuffled = pd.concat(parts)
        left = _day_weighted(shuffled[shuffled._fake == 1])
        right = _day_weighted(shuffled[shuffled._fake == 0])
        if left is not None and right is not None:
            diffs.append(left - right)
    if not diffs:
        return (None, None)
    return (round(min(diffs), 4), round(max(diffs), 4))


def _contrast(frame: pd.DataFrame, flag: str, label: str) -> dict[str, Any]:
    treated = frame[frame[flag] == 1]
    control = frame[frame[flag] == 0]
    if len(treated) < MIN_GROUP or len(control) < MIN_GROUP:
        return {
            "label": label,
            "verdict": "样本不足",
            "n_treated": len(treated),
            "n_control": len(control),
            "min_group": MIN_GROUP,
        }
    left = _day_weighted(treated)
    right = _day_weighted(control)
    paired = _paired_by_day(frame, flag)
    diffs = (paired.treated - paired.control).tolist() if not paired.empty else []
    ci = _paired_bootstrap_ci(diffs, random.Random(0))
    band = _intraday_shuffle_band(frame, flag)
    raw_diff = None if left is None or right is None else round(left - right, 4)
    paired_diff = round(sum(diffs) / len(diffs), 4) if diffs else None
    inside = None
    if paired_diff is not None and band[0] is not None:
        inside = band[0] <= paired_diff <= band[1]
    return {
        "label": label,
        "n_treated": len(treated),
        "n_control": len(control),
        "treated_ret": None if left is None else round(left, 4),
        "control_ret": None if right is None else round(right, 4),
        "raw_diff": raw_diff,
        "paired_days": len(paired),
        "paired_diff": paired_diff,
        "paired_bootstrap_95ci": list(ci),
        "intraday_shuffle_band": list(band),
        "paired_diff_inside_noise": inside,
        "verdict": _read_verdict(paired_diff, ci, inside),
    }


def _read_verdict(diff: float | None, ci: tuple[float | None, float | None], inside: bool | None) -> str:
    if diff is None:
        return "样本不足"
    if inside:
        return "落在噪声带宽内：无区分力"
    if ci[0] is not None and ci[0] < 0 < ci[1]:
        return "超出噪声但 CI 跨 0：处于噪声边缘，不足以支持接线"
    return "正向、超出噪声" if diff > 0 else "负向、超出噪声（不可用于晋级）"


def main() -> int:
    args = parse_args()
    from integrations.supabase_base import create_admin_client, is_admin_configured

    if not is_admin_configured():
        print("需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        return 1
    client = create_admin_client()
    outcomes = _fetch_all(client, "signal_outcomes")
    health = _fetch_all(client, "signal_health_daily")
    observations = _fetch_all(client, "signal_observations")
    report = build_report(outcomes, health, observations, horizon=int(args.horizon))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"capital_context_alpha_h{args.horizon}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n[alpha] written -> {path}")
    return 0


def build_report(
    outcomes: pd.DataFrame,
    health: pd.DataFrame,
    observations: pd.DataFrame,
    *,
    horizon: int,
) -> dict[str, Any]:
    matured = outcomes[(outcomes.horizon_days == horizon) & (outcomes.status == "done")].copy()
    matured["d"] = pd.to_datetime(matured.trade_date)
    merged = _merge_prior_health(matured, health, horizon)

    by_state = {
        str(state): {
            "n": len(group),
            "ret": None if _day_weighted(group) is None else round(_day_weighted(group), 4),
            "win_rate_pct": round(100.0 * float((group.return_pct > 0).mean()), 2),
        }
        for state, group in merged.groupby("health_state")
        if len(group) >= MIN_GROUP
    }
    health_result = _contrast(merged, "healthy", "HEALTHY vs 其他")
    dominant = _dominant_signal_check(merged)
    correlations = _health_correlations(merged)
    capital = _capital_contrast(matured, observations)
    return {
        "horizon_days": horizon,
        "window": {"start": str(matured.trade_date.min()), "end": str(matured.trade_date.max())},
        "matured_outcomes": len(matured),
        "baseline_ret": None if _day_weighted(matured) is None else round(_day_weighted(matured), 4),
        # 净收益：signal_outcomes.return_pct 是毛收益，不含佣金/印花税/过户费/滑点。
        # 判断「值不值得做」必须看这一行。
        "baseline_net_ret": net_return_pct(_day_weighted(matured)),
        "round_trip_cost_pct": round(round_trip_cost_pct(), 4),
        "baseline_win_rate_pct": round(100.0 * float((matured.return_pct > 0).mean()), 2),
        "health": {
            "matched_outcomes": len(merged),
            "by_state": by_state,
            "contrast": health_result,
            "dominant_signal_check": dominant,
            "correlation_history_vs_forward": correlations,
        },
        "capital_context": capital,
    }


_HEALTH_COLUMNS = ("d", "signal_type", "regime", "health_state", "sample_count", "avg_return_pct")


def _merge_prior_health(matured: pd.DataFrame, health: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """给每条 outcome 附上其交易日**之前**最近一期的 health。

    ``allow_exact_matches=False`` 是关键：当日的 health 快照包含当日结果，用它就是未来信息。
    health 表为空（首次运行或未回填）时返回空表而不是抛错，让报告仍能给出基线与覆盖率。
    """
    empty = pd.DataFrame(columns=[*matured.columns, "health_state", "sample_count", "avg_return_pct", "healthy"])
    if health.empty or "horizon_days" not in health.columns:
        return empty
    health_h = health[health.horizon_days == horizon].copy()
    if health_h.empty:
        return empty
    health_h["d"] = pd.to_datetime(health_h.as_of_date)
    merged = pd.merge_asof(
        matured.sort_values("d")[["d", "code", "signal_type", "regime", "return_pct", "max_drawdown_pct"]],
        health_h.sort_values("d")[list(_HEALTH_COLUMNS)],
        on="d",
        by=["signal_type", "regime"],
        direction="backward",
        allow_exact_matches=False,
    ).dropna(subset=["health_state"])
    merged["healthy"] = (merged.health_state == "HEALTHY").astype(int)
    return merged


def _dominant_signal_check(merged: pd.DataFrame) -> dict[str, Any]:
    """剔除占比最大的信号后重算：判断结论是否由单一信号驱动。"""
    treated = merged[merged.healthy == 1]
    if treated.empty:
        return {"verdict": "无 HEALTHY 样本"}
    counts = treated.signal_type.value_counts()
    top = str(counts.index[0])
    share = round(100.0 * float(counts.iloc[0]) / len(treated), 2)
    rest = merged[merged.signal_type != top]
    return {
        "dominant_signal": top,
        "share_pct": share,
        "excluding_dominant": _contrast(rest, "healthy", f"HEALTHY vs 其他（剔除 {top}）"),
    }


def _health_correlations(merged: pd.DataFrame) -> dict[str, Any]:
    frame = merged.dropna(subset=["avg_return_pct"])
    if len(frame) < MIN_GROUP:
        return {"verdict": "样本不足"}
    corr = float(frame.avg_return_pct.corr(frame.return_pct))
    return {
        "n": len(frame),
        "corr_history_avg_return_vs_forward": round(corr, 4),
        "reading": "负值意味着历史统计好的信号后续更可能回落（均值回复），不宜用于晋级"
        if corr < 0
        else "正值才支持用历史 health 预测未来",
    }


def _capital_contrast(matured: pd.DataFrame, observations: pd.DataFrame) -> dict[str, Any]:
    """带资金佐证 vs 不带，按 (code, trade_date) 关联 observation。"""
    if observations.empty or "features_json" not in observations.columns:
        return {"verdict": "无 observation 数据"}
    marks = []
    for _, row in observations.iterrows():
        features = _load_json(row.get("features_json"))
        context = features.get("source_context") if isinstance(features, dict) else None
        providers = sorted(
            k for k in (context or {}) if k not in {"version", "fetched_at", "trade_date", "source_status"}
        )
        marks.append(
            {
                "code": str(row.get("code") or "").strip(),
                "trade_date": str(row.get("trade_date") or "").strip(),
                "has_capital": 1 if providers else 0,
                "providers": ",".join(providers),
            }
        )
    marked = pd.DataFrame(marks)
    coverage = round(100.0 * float(marked.has_capital.mean()), 2) if not marked.empty else 0.0
    frame = matured.merge(marked, on=["code", "trade_date"], how="inner")
    provider_counts = (
        marked[marked.has_capital == 1].providers.value_counts().head(10).to_dict() if not marked.empty else {}
    )
    result: dict[str, Any] = {
        "observations": len(marked),
        "with_capital": int(marked.has_capital.sum()) if not marked.empty else 0,
        "coverage_pct": coverage,
        "provider_combinations": provider_counts,
        "joined_outcomes": len(frame),
    }
    if frame.empty:
        result["verdict"] = "无法关联 outcome"
        return result
    result["contrast"] = _contrast(frame, "has_capital", "带资金佐证 vs 不带")
    return result


def _load_json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


if __name__ == "__main__":
    raise SystemExit(main())
