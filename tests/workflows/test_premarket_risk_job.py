from __future__ import annotations

from workflows import premarket_risk_job as job


def _snapshot() -> job.PremarketSnapshot:
    return job.PremarketSnapshot(
        a50={"ok": True, "source": "akshare", "date": "2026-06-22", "close": 13000, "pct_chg": -1.2},
        vix={"ok": False, "source": "stooq", "date": "2026-06-19", "close": 22.5, "pct_chg": 9.0, "error": "stale"},
        regime="RISK_OFF",
        reasons=["A50跌幅 -1.20% <= -1.00%"],
        public_brief={
            "banner_title": "盘前风险偏谨慎",
            "banner_message": "隔夜外部波动放大，观察开盘承接。",
            "banner_tone": "谨慎",
            "llm_used": True,
            "provider": "efficiency",
            "model": "eff",
            "validation_reasons": [],
        },
        action_lines=["动作矩阵", "- PROBE`：默认禁止"],
    )


def test_build_premarket_content_includes_public_brief_and_warnings() -> None:
    content = job.build_premarket_content(_snapshot())

    assert "**结论**: `RISK_OFF`" in content
    assert "**公共总结**: 盘前风险偏谨慎" in content
    assert "**VIX注意**: stale" in content
    assert "不执行选股和下单" in content


def test_build_market_signal_patch_preserves_public_fields() -> None:
    patch = job.build_market_signal_patch(_snapshot())

    assert patch["premarket_regime"] == "RISK_OFF"
    assert patch["a50_pct_chg"] == -1.2
    assert patch["vix_source"] == "stooq"
    assert patch["banner_title"] == "盘前风险偏谨慎"
    assert patch["source_jobs"]["premarket_risk_job"]["writer"] == "a50_vix_risk"
    assert patch["source_jobs"]["premarket_risk_job"]["public_brief"]["provider"] == "efficiency"


def test_run_premarket_dry_run_skips_persist_and_notification(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(job, "collect_premarket_snapshot", lambda _logs_path: _snapshot())
    monkeypatch.setattr(
        job,
        "persist_premarket_signal",
        lambda *_args: (_ for _ in ()).throw(AssertionError("persist should be skipped")),
    )
    monkeypatch.setattr(
        job,
        "send_premarket_notification",
        lambda *_args: (_ for _ in ()).throw(AssertionError("notify should be skipped")),
    )

    code = job.run_premarket_risk_job(
        job.PremarketRiskJobConfig(logs_path=str(tmp_path / "premarket.log"), webhook="https://feishu", dry_run=True)
    )

    assert code == 0
    assert "不发送飞书" in (tmp_path / "premarket.log").read_text(encoding="utf-8")


def test_send_premarket_notification_treats_missing_webhook_as_skip(tmp_path) -> None:
    code = job.send_premarket_notification("", "content", str(tmp_path / "premarket.log"))

    assert code == 0
    assert "FEISHU_WEBHOOK_URL 未配置" in (tmp_path / "premarket.log").read_text(encoding="utf-8")
