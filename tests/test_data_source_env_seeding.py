from __future__ import annotations

import json
from pathlib import Path

from integrations import local_auth


def _use_config(monkeypatch, tmp_path: Path, payload: dict) -> None:
    path = tmp_path / "wyckoff.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(local_auth, "CONFIG_FILE", path)
    monkeypatch.setattr(local_auth, "_OLD_CONFIG_FILE", tmp_path / "missing.json")


def test_seed_fills_missing_env_and_keeps_explicit_values(monkeypatch, tmp_path: Path) -> None:
    _use_config(monkeypatch, tmp_path, {"tushare_token": " from-config ", "tickflow_api_key": "tk-config"})
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setenv("TICKFLOW_API_KEY", "tk-explicit")

    local_auth.seed_data_source_env()

    import os

    assert os.environ["TUSHARE_TOKEN"] == "from-config"
    assert os.environ["TICKFLOW_API_KEY"] == "tk-explicit"


def test_seed_ignores_blank_config_values(monkeypatch, tmp_path: Path) -> None:
    _use_config(monkeypatch, tmp_path, {"tushare_token": "  ", "tickflow_api_key": None})
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)

    local_auth.seed_data_source_env()

    import os

    assert "TUSHARE_TOKEN" not in os.environ
    assert "TICKFLOW_API_KEY" not in os.environ


def test_public_mcp_backend_seeds_local_data_source_credentials(monkeypatch, tmp_path: Path) -> None:
    """登录用户的 get_credential 只查云端，MCP 必须和 CLI 一样先把本机数据源配置灌进环境。"""
    from integrations import local_db
    from integrations.public_mcp import backend

    _use_config(monkeypatch, tmp_path, {"tushare_token": "ts-config"})
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setattr(local_db, "init_db", lambda: None)
    monkeypatch.setattr(backend, "build_context", lambda: None)

    backend.DomainBackend()

    import os

    assert os.environ["TUSHARE_TOKEN"] == "ts-config"
