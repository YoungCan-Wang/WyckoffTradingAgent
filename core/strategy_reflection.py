"""Build data-backed strategy reflection payloads."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from utils.safe import safe_float as _safe_float

# 定「哪条 track 更强」所需的最小样本。低于此数不出结论——「样本不足」和
# 「已比较过、这条更强」是两回事,后者会被人拿去调车道权重。
MIN_TRACK_SAMPLES = 30


def _done_rows(outcomes: list[dict[str, Any]], horizon_days: int) -> list[dict[str, Any]]:
    horizon = int(horizon_days)
    return [
        row
        for row in outcomes
        if int(row.get("horizon_days") or 0) == horizon and str(row.get("status") or "").lower() == "done"
    ]


def _track_of(row: dict[str, Any]) -> str:
    track = str(row.get("track") or "").strip()
    if track:
        return track
    signal = str(row.get("signal_type") or "").strip().lower()
    return "Trend" if signal in {"sos", "evr", "trend_pullback"} else "Accum"


def summarize_track_performance(outcomes: list[dict[str, Any]], horizon_days: int) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in _done_rows(outcomes, horizon_days):
        key = (_track_of(row), str(row.get("regime") or "ALL").strip().upper() or "ALL")
        buckets.setdefault(key, []).append(row)
    summary = []
    for (track, regime), rows in sorted(buckets.items()):
        returns = [_safe_float(row.get("return_pct")) for row in rows]
        drawdowns = [_safe_float(row.get("max_drawdown_pct")) for row in rows]
        wins = sum(ret > 0 for ret in returns)
        summary.append(
            {
                "track": track,
                "regime": regime,
                "sample_count": len(rows),
                "win_rate": round(wins / len(rows), 4) if rows else 0.0,
                "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else 0.0,
                "avg_drawdown_pct": round(sum(drawdowns) / len(drawdowns), 4) if drawdowns else 0.0,
            }
        )
    return summary


def summarize_shadow_runs(shadow_runs: list[dict[str, Any]]) -> dict[str, Any]:
    added = sum(len(row.get("diff_added") or []) for row in shadow_runs)
    removed = sum(len(row.get("diff_removed") or []) for row in shadow_runs)
    return {
        "run_count": len(shadow_runs),
        "added_count": added,
        "removed_count": removed,
        "avg_added": round(added / len(shadow_runs), 4) if shadow_runs else 0.0,
        "avg_removed": round(removed / len(shadow_runs), 4) if shadow_runs else 0.0,
    }


def aggregate_by_track(track_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 (track, regime) 逐格明细按样本数加权合成 track 级汇总。

    ``track_summary`` 的每一行是一个 regime 格子,直接在格子上取 max 会拿单格当结论。
    这里按 ``sample_count`` 加权还原 track 整体,均值才和「这条车道整体如何」同口径。
    """
    agg: dict[str, dict[str, float]] = {}
    for row in track_summary:
        n = int(row.get("sample_count") or 0)
        if n <= 0:
            continue
        acc = agg.setdefault(str(row.get("track") or ""), {"n": 0.0, "ret": 0.0, "win": 0.0})
        acc["n"] += n
        acc["ret"] += _safe_float(row.get("avg_return_pct")) * n
        acc["win"] += _safe_float(row.get("win_rate")) * n
    out = []
    for track, acc in agg.items():
        n = acc["n"]
        out.append(
            {
                "track": track,
                "sample_count": int(n),
                "win_rate": round(acc["win"] / n, 4),
                "avg_return_pct": round(acc["ret"] / n, 4),
            }
        )
    return sorted(out, key=lambda row: -row["sample_count"])


def _best_track(track_summary: list[dict[str, Any]]) -> str:
    """样本够的 track 里按胜率优先挑一条;都不够就返回空字符串。

    原先两处都错:①``sample_count > 0`` 等于没有门槛,n=1 的格子也能定结论;
    ②取 max 的单位是 (track, regime) 单格,却当成「整条 track 最强」写进结论。
    2026-09-05 那轮就这样翻了号:Accum/CRASH 单格 n=26 / +3.77% 被选中,
    而按样本加权 Accum 整体 -0.607% / 胜率 38.2%,比 Trend 的 -0.094% / 41.6% 更差,
    报告却写着「Accum track has the strongest recent outcome profile」。

    排序键用胜率优先(其次平均收益)——第一优先级是胜率,不是幅度。
    """
    eligible = [row for row in aggregate_by_track(track_summary) if row["sample_count"] >= MIN_TRACK_SAMPLES]
    if not eligible:
        return ""
    best = max(eligible, key=lambda row: (row["win_rate"], row["avg_return_pct"]))
    return str(best.get("track") or "")


def _reflection_text(track_summary: list[dict[str, Any]], shadow_summary: dict[str, Any]) -> str:
    if not track_summary:
        return "样本不足，保持 shadow 观察，不调整生产策略。"
    shadow_part = (
        f"Shadow runs={shadow_summary.get('run_count', 0)}, "
        f"avg_added={shadow_summary.get('avg_added', 0)}, avg_removed={shadow_summary.get('avg_removed', 0)}. "
        "Keep candidate in review; do not auto-promote."
    )
    best = _best_track(track_summary)
    if not best:
        # 没有一条 track 够样本。这里必须说「不足」,不能退回 unknown 之类看着像结论的词。
        return f"No track reaches {MIN_TRACK_SAMPLES} samples; not ranking tracks this round. {shadow_part}"
    agg = {row["track"]: row for row in aggregate_by_track(track_summary)}
    hit = agg[best]
    # 带上数和样本量。两条车道可能都是负的,「最强」只是「亏得少」,不写数就会被读成赚钱。
    return (
        f"{best} track leads on win rate ({hit['win_rate'] * 100:.1f}%, "
        f"avg_return={hit['avg_return_pct']:+.3f}%, n={hit['sample_count']}), "
        f"sample-weighted across regimes. {shadow_part}"
    )


def build_strategy_reflection(
    outcomes: list[dict[str, Any]],
    shadow_runs: list[dict[str, Any]],
    *,
    market: str = "cn",
    as_of_date: str | None = None,
    horizon_days: int = 5,
) -> dict[str, Any]:
    track_summary = summarize_track_performance(outcomes, horizon_days)
    shadow_summary = summarize_shadow_runs(shadow_runs)
    now_iso = datetime.now(UTC).isoformat()
    return {
        "market": market,
        "as_of_date": as_of_date or date.today().isoformat(),
        "horizon_days": int(horizon_days),
        "status": "SHADOW",
        "summary": {
            "track_performance": track_summary,
            # 逐格明细容易被拿单格当结论,这里同时落 track 级加权汇总,读的人不用自己算。
            "track_totals": aggregate_by_track(track_summary),
            "shadow": shadow_summary,
            "preferred_track": _best_track(track_summary),
        },
        "reflection_text": _reflection_text(track_summary, shadow_summary),
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def build_policy_candidate(reflection: dict[str, Any]) -> dict[str, Any] | None:
    summary = reflection.get("summary") if isinstance(reflection.get("summary"), dict) else {}
    preferred_track = str(summary.get("preferred_track") or "").strip()
    if not preferred_track:
        return None
    now_iso = datetime.now(UTC).isoformat()
    return {
        "market": reflection["market"],
        "as_of_date": reflection["as_of_date"],
        "status": "READY_FOR_REVIEW",
        "source_reflection_date": reflection["as_of_date"],
        "candidate_policy": {
            "mode": "shadow",
            "preferred_track": preferred_track,
            "horizon_days": reflection["horizon_days"],
            "auto_promote": False,
        },
        "validation_summary": summary,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
