from __future__ import annotations

import pytest

from cli.__main__ import _build_parser, _cmd_report


def test_cmd_report_prints_generated_report(monkeypatch, capsys) -> None:
    from agents import stock_data_helpers
    from cli import auth
    from integrations import local_db
    from workflows import step3_batch_report

    captured: dict = {}
    monkeypatch.setattr(local_db, "init_db", lambda: None)
    monkeypatch.setattr(
        auth,
        "load_model_configs",
        lambda: [{"id": "m1", "provider_name": "openai", "api_key": "key", "model": "gpt-test", "base_url": ""}],
    )
    monkeypatch.setattr(auth, "load_default_model_id", lambda: "m1")
    monkeypatch.setattr(
        stock_data_helpers, "code_to_name", lambda code: {"002293": "罗莱生活", "001314": "亿道信息"}[code]
    )

    def fake_run_report(**kwargs):
        captured.update(kwargs)
        return True, "ok", "# 研报\n002293 罗莱生活：重点观察"

    monkeypatch.setattr(step3_batch_report, "run", fake_run_report)

    args = _build_parser().parse_args(["report", "002293,001314"])
    _cmd_report(args)

    out = capsys.readouterr().out
    assert captured["symbols_info"] == [
        {"code": "002293", "name": "罗莱生活", "tag": "", "selection_source": "explicit_report_input"},
        {"code": "001314", "name": "亿道信息", "tag": "", "selection_source": "explicit_report_input"},
    ]
    assert captured["notify"] is False
    assert "✓ 研报生成完成" in out
    assert "--- 研报正文 ---" in out
    assert "002293 罗莱生活：重点观察" in out


def test_cmd_report_exits_nonzero_when_report_generation_fails(monkeypatch, capsys) -> None:
    from cli import auth
    from integrations import local_db
    from workflows import step3_batch_report

    monkeypatch.setattr(local_db, "init_db", lambda: None)
    monkeypatch.setattr(
        auth,
        "load_model_configs",
        lambda: [{"id": "m1", "provider_name": "openai", "api_key": "key", "model": "gpt-test", "base_url": ""}],
    )
    monkeypatch.setattr(auth, "load_default_model_id", lambda: "m1")
    monkeypatch.setattr(step3_batch_report, "run", lambda **_kwargs: (False, "llm_failed", ""))

    args = _build_parser().parse_args(["report", "002293"])
    with pytest.raises(SystemExit) as exc:
        _cmd_report(args)

    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "✗ 研报生成失败: llm_failed" in out
