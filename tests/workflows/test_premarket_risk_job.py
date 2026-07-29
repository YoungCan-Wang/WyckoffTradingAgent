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


def _backstop(monkeypatch, tmp_path, *, row, trading_day=True):
    monkeypatch.setattr(job, "is_a_share_trading_day", lambda _d: trading_day)
    monkeypatch.setattr(job, "load_market_signal_daily", lambda _d: row)
    logs = tmp_path / "premarket.log"
    return job.backstop_should_skip(str(logs)), logs


def test_backstop_reruns_when_the_dispatch_trigger_missed_the_day(monkeypatch, tmp_path) -> None:
    """外部触发器连续 4 个周一周二未触发，Step4 因此降级 UNKNOWN 禁买——兜底就是为这个存在。"""
    skip, logs = _backstop(monkeypatch, tmp_path, row={"benchmark_regime": "NEUTRAL"})

    assert skip is False
    assert "盘前态缺失，补跑" in logs.read_text(encoding="utf-8")


def test_backstop_stays_quiet_when_the_dispatch_trigger_already_wrote_today(monkeypatch, tmp_path) -> None:
    """盘前任务无条件推飞书，不短路就会在触发正常的日子天天多一条中午的重复推送。"""
    skip, logs = _backstop(monkeypatch, tmp_path, row={"premarket_regime": "NORMAL"})

    assert skip is True
    assert "已存在(NORMAL)" in logs.read_text(encoding="utf-8")


def test_backstop_skips_holidays_because_step4_does_not_run_then(monkeypatch, tmp_path) -> None:
    skip, logs = _backstop(monkeypatch, tmp_path, row=None, trading_day=False)

    assert skip is True
    assert "非A股交易日" in logs.read_text(encoding="utf-8")


def test_backstop_runs_the_job_when_the_readback_fails(monkeypatch, tmp_path) -> None:
    """读库失败按缺失处理：重复推送只是噪音，漏掉盘前态要赔上一整天的开仓能力。"""
    monkeypatch.setattr(job, "is_a_share_trading_day", lambda _d: True)
    monkeypatch.setattr(
        job, "load_market_signal_daily", lambda _d: (_ for _ in ()).throw(RuntimeError("connection reset"))
    )
    logs = tmp_path / "premarket.log"

    assert job.backstop_should_skip(str(logs)) is False
    assert "读库失败" in logs.read_text(encoding="utf-8")


def test_normal_mode_never_short_circuits(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        job,
        "backstop_should_skip",
        lambda *_args: (_ for _ in ()).throw(AssertionError("非兜底模式不该做幂等检查")),
    )
    monkeypatch.setattr(job, "collect_premarket_snapshot", lambda _logs_path: _snapshot())

    code = job.run_premarket_risk_job(
        job.PremarketRiskJobConfig(logs_path=str(tmp_path / "premarket.log"), dry_run=True)
    )

    assert code == 0


def test_backstop_short_circuit_skips_vix_polling_and_the_feishu_push(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(job, "backstop_should_skip", lambda *_args: True)
    for name in ("collect_premarket_snapshot", "persist_premarket_signal", "send_premarket_notification"):
        monkeypatch.setattr(job, name, lambda *_a, _n=name: (_ for _ in ()).throw(AssertionError(f"{_n} 不该被调用")))

    code = job.run_premarket_risk_job(
        job.PremarketRiskJobConfig(logs_path=str(tmp_path / "premarket.log"), webhook="https://feishu", backstop=True)
    )

    assert code == 0


def test_send_premarket_notification_treats_missing_webhook_as_skip(tmp_path) -> None:
    code = job.send_premarket_notification("", "content", str(tmp_path / "premarket.log"))

    assert code == 0
    assert "FEISHU_WEBHOOK_URL 未配置" in (tmp_path / "premarket.log").read_text(encoding="utf-8")
