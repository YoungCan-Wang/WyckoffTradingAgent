from __future__ import annotations


def test_upgrade_package_cmd_targets_browser_extra(monkeypatch):
    from cli import __main__ as main

    monkeypatch.setattr(main.sys, "executable", "/tmp/wyckoff-venv/bin/python")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uv")
    cmd = main._upgrade_package_cmd()
    assert cmd[:3] == ["/usr/bin/uv", "pip", "install"]
    assert "--python" in cmd
    assert "/tmp/wyckoff-venv/bin/python" in cmd
    assert "youngcan-wyckoff-analysis[browser]" in cmd


def test_upgrade_package_cmd_falls_back_to_pip(monkeypatch):
    from cli import __main__ as main

    monkeypatch.setattr(main.sys, "executable", "/tmp/wyckoff-venv/bin/python")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    cmd = main._upgrade_package_cmd()
    assert cmd[:4] == ["/tmp/wyckoff-venv/bin/python", "-m", "pip", "install"]
    assert "youngcan-wyckoff-analysis[browser]" in cmd
