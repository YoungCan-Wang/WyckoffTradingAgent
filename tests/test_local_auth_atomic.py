import json
import subprocess
import sys
import time

import pytest

from integrations import local_auth


def _point_config_at(monkeypatch: pytest.MonkeyPatch, path) -> None:
    monkeypatch.setattr(local_auth, "CONFIG_FILE", path)
    monkeypatch.setattr(local_auth, "_OLD_CONFIG_FILE", path.with_name("old-config.json"))


def test_config_update_waits_for_other_process_and_preserves_fields(tmp_path, monkeypatch):
    config = tmp_path / "wyckoff.json"
    _point_config_at(monkeypatch, config)
    child = """
import sys
from pathlib import Path
from integrations import local_auth
local_auth.CONFIG_FILE = Path(sys.argv[1])
local_auth._OLD_CONFIG_FILE = Path(sys.argv[1]).with_name('old-config.json')
local_auth.save_config_key('password', 'saved')
"""

    with local_auth._write_lock(config, local_auth._CONFIG_THREAD_LOCK):
        process = subprocess.Popen([sys.executable, "-c", child, str(config)])
        time.sleep(0.2)
        assert process.poll() is None, "另一个进程必须等待配置事务完成"
        local_auth._atomic_write_json(
            config,
            {"models": [{"id": "ox", "model": "stealth/ox-alpha"}], "default": "ox"},
            indent=2,
        )

    assert process.wait(timeout=10) == 0
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["password"] == "saved"
    assert saved["default"] == "ox"
    assert saved["models"][0]["model"] == "stealth/ox-alpha"


def test_malformed_config_is_never_replaced_with_partial_data(tmp_path, monkeypatch):
    config = tmp_path / "wyckoff.json"
    config.write_text('{"models": [', encoding="utf-8")
    _point_config_at(monkeypatch, config)

    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        local_auth.save_config_key("password", "saved")

    assert config.read_text(encoding="utf-8") == '{"models": ['


def test_legacy_model_migration_keeps_unrelated_settings():
    migrated = local_auth._migrate_config(
        {
            "provider_name": "openai",
            "api_key": "secret",
            "model": "model-a",
            "base_url": "https://example.test/v1",
            "email": "me@example.test",
            "desktop_tone": "concise",
        }
    )

    assert migrated["email"] == "me@example.test"
    assert migrated["desktop_tone"] == "concise"
    assert migrated["default"] == "openai"
