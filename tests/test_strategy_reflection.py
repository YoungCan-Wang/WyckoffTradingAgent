from __future__ import annotations

from core.strategy_reflection import MIN_TRACK_SAMPLES


def _rows(track: str, regime: str, count: int, return_pct: float) -> list[dict]:
    return [
        {"track": track, "regime": regime, "horizon_days": 5, "status": "done", "return_pct": return_pct}
        for _ in range(count)
    ]


def test_build_strategy_reflection_and_candidate():
    from core.strategy_reflection import build_policy_candidate, build_strategy_reflection

    # 两条 track 都过 MIN_TRACK_SAMPLES,Accum 全胜、Trend 全负,排序键该选 Accum。
    outcomes = _rows("Trend", "RISK_ON", 40, -1.0) + _rows("Accum", "RISK_ON", 40, 3.0)
    shadow_runs = [{"diff_added": ["000001"], "diff_removed": ["000002", "000003"]}]

    reflection = build_strategy_reflection(outcomes, shadow_runs, market="cn", as_of_date="2026-06-12")
    candidate = build_policy_candidate(reflection)

    assert reflection["status"] == "SHADOW"
    assert reflection["summary"]["preferred_track"] == "Accum"
    assert reflection["summary"]["shadow"]["avg_removed"] == 2.0
    assert candidate is not None
    assert candidate["status"] == "READY_FOR_REVIEW"
    assert candidate["candidate_policy"]["auto_promote"] is False


def test_strategy_reflection_job_dry_run_payload(monkeypatch):
    import workflows.strategy_reflection_job as job

    request = job.StrategyReflectionRequest(
        market="cn",
        as_of_date="2026-06-12",
        horizon_days=5,
        outcome_days=180,
        shadow_days=30,
        limit=100,
    )
    monkeypatch.setattr(job, "load_recent_signal_outcomes", lambda *_args: _rows("Trend", "ALL", 40, 2.0))
    monkeypatch.setattr(job, "load_policy_shadow_runs", lambda *_args: [{"diff_added": [], "diff_removed": []}])

    reflection, candidate = job.build_strategy_reflection_payloads(request)

    assert reflection["as_of_date"] == "2026-06-12"
    assert reflection["summary"]["preferred_track"] == "Trend"
    assert candidate is not None
    assert candidate["status"] == "READY_FOR_REVIEW"


def test_single_strong_regime_cell_does_not_decide_the_track():
    """真实缺陷的回归:一个小格子不能代表整条 track。

    2026-09-05 那轮 Accum/CRASH 单格 n=26 / +3.77% 被选成「最强 track」,
    而按样本加权 Accum 整体比 Trend 更差(-0.607% / 38.2% vs -0.094% / 41.6%)。
    这里复刻那个形状:Accum 有一个亮眼小格 + 一个很差的大格,整体弱于 Trend。
    """
    from core.strategy_reflection import build_strategy_reflection

    outcomes = (
        _rows("Accum", "CRASH", 26, 3.8)  # 亮眼但小
        + _rows("Accum", "RISK_OFF", 300, -1.0)  # 拖累且大
        + _rows("Trend", "NEUTRAL", 500, 0.2)  # 平淡但整体更好
    )
    reflection = build_strategy_reflection(outcomes, [], market="cn", as_of_date="2026-09-05")

    totals = {row["track"]: row for row in reflection["summary"]["track_totals"]}
    assert totals["Accum"]["avg_return_pct"] < totals["Trend"]["avg_return_pct"]
    assert reflection["summary"]["preferred_track"] == "Trend"
    assert "Accum" not in reflection["reflection_text"]


def test_below_sample_floor_reports_insufficient_not_a_winner():
    """样本不够时必须说「不足」,而不是挑一条出来当结论,也不该生成候选。"""
    from core.strategy_reflection import build_policy_candidate, build_strategy_reflection

    outcomes = _rows("Trend", "ALL", 3, 5.0) + _rows("Accum", "ALL", 2, -5.0)
    reflection = build_strategy_reflection(outcomes, [], market="cn", as_of_date="2026-06-12")

    assert reflection["summary"]["preferred_track"] == ""
    assert f"No track reaches {MIN_TRACK_SAMPLES} samples" in reflection["reflection_text"]
    assert "strongest" not in reflection["reflection_text"]
    assert build_policy_candidate(reflection) is None


def test_win_rate_leads_the_ranking_key():
    """排序键是胜率优先——第一优先级是胜率,不是幅度。

    Trend 胜率更高但均值被一次大亏拖低;Accum 均值更高、胜率更低。选 Trend。
    """
    from core.strategy_reflection import build_strategy_reflection

    outcomes = (
        _rows("Trend", "ALL", 39, 1.0)
        + _rows("Trend", "ALL", 1, -100.0)  # 拉低均值,不改胜率排序
        + _rows("Accum", "ALL", 20, 12.0)
        + _rows("Accum", "ALL", 20, -1.0)
    )
    reflection = build_strategy_reflection(outcomes, [], market="cn", as_of_date="2026-06-12")

    totals = {row["track"]: row for row in reflection["summary"]["track_totals"]}
    assert totals["Accum"]["avg_return_pct"] > totals["Trend"]["avg_return_pct"]
    assert totals["Trend"]["win_rate"] > totals["Accum"]["win_rate"]
    assert reflection["summary"]["preferred_track"] == "Trend"


def test_track_totals_are_sample_weighted_not_cell_averaged():
    """加权口径校验:两个大小差很多的格子,汇总必须贴近大格子而非两格算术平均。"""
    from core.strategy_reflection import aggregate_by_track, summarize_track_performance

    outcomes = _rows("Trend", "A", 90, 1.0) + _rows("Trend", "B", 10, -9.0)
    totals = aggregate_by_track(summarize_track_performance(outcomes, 5))

    assert len(totals) == 1
    assert totals[0]["sample_count"] == 100
    assert totals[0]["avg_return_pct"] == 0.0  # (90*1 + 10*-9)/100 = 0.0,算术平均会得 -4.0
    assert totals[0]["win_rate"] == 0.9
