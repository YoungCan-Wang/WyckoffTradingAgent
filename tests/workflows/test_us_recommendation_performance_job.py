from __future__ import annotations


def test_us_recommendation_performance_job_passes_refresh_options(monkeypatch):
    import workflows.us_recommendation_performance_job as job

    captured: dict[str, int] = {}
    monkeypatch.setattr(
        job,
        "refresh_us_tracking_performance",
        lambda **kwargs: captured.update(kwargs) or _summary(rows_total=3, rows_updated=2),
    )

    result = job.run_us_recommendation_performance_job(
        job.UsRecommendationPerformanceRequest(max_dates=4, kline_count=9)
    )

    assert result == 0
    assert captured == {"max_dates": 4, "kline_count": 9}


def test_us_recommendation_performance_job_reports_failure(monkeypatch, tmp_path):
    import workflows.us_recommendation_performance_job as job

    logs_path = tmp_path / "us-performance.log"

    def fail_refresh(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(job, "refresh_us_tracking_performance", fail_refresh)

    result = job.run_us_recommendation_performance_job(job.UsRecommendationPerformanceRequest(logs_path=str(logs_path)))

    assert result == 1
    assert "任务失败: boom" in logs_path.read_text(encoding="utf-8")


def _summary(**overrides):
    summary = {
        "rows_total": 0,
        "rows_updated": 0,
        "rows_skipped": 0,
        "codes_total": 0,
        "codes_no_data": 0,
        "latest_trade_date": "20260622",
        "mfe_ge_5": 0,
        "mfe_ge_10": 0,
        "mae_le_neg5": 0,
    }
    summary.update(overrides)
    return summary
