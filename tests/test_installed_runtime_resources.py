from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("agents", "cli", "core", "integrations", "tools", "utils", "workflows")


def test_market_universe_pair_uses_one_complete_installed_directory(monkeypatch, tmp_path: Path) -> None:
    from utils import package_resources

    override = tmp_path / "override"
    override.mkdir()
    (override / "us.txt").touch()
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "us.txt").touch()
    (installed / "us_meta.json").touch()
    cwd_universe = tmp_path / "data" / "market_universes"
    cwd_universe.mkdir(parents=True)
    (cwd_universe / "us.txt").touch()
    (cwd_universe / "us_meta.json").touch()

    monkeypatch.setenv("MARKET_UNIVERSE_DIR", str(override))
    monkeypatch.setattr(package_resources, "_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(package_resources, "_installed_universe_dir", lambda: installed)
    monkeypatch.chdir(tmp_path)

    assert package_resources.market_universe_dir("us.txt", "us_meta.json") == installed


def test_market_metadata_falls_back_per_market_for_partial_override(monkeypatch, tmp_path: Path) -> None:
    from tools import market_universe_meta
    from utils import package_resources

    override = tmp_path / "override"
    override.mkdir()
    (override / "us_meta.json").write_text("[]", encoding="utf-8")
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "hk_meta.json").write_text(
        json.dumps([{"symbol": "00700.HK", "name": "Tencent"}]),
        encoding="utf-8",
    )

    monkeypatch.setenv("MARKET_UNIVERSE_DIR", str(override))
    monkeypatch.setattr(package_resources, "_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(package_resources, "_installed_universe_dir", lambda: installed)
    monkeypatch.chdir(tmp_path)
    market_universe_meta.load_all_market_meta.cache_clear()
    try:
        assert market_universe_meta.load_symbol_name_map(("hk",))["00700.HK"] == "Tencent"
    finally:
        market_universe_meta.load_all_market_meta.cache_clear()


def test_installed_runtime_resources_load_from_share_directory(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    for package in PACKAGES:
        shutil.copytree(ROOT / package, site_packages / package)
    (site_packages / "pyproject.toml").write_text('[project]\nname = "unrelated-dependency"\n', encoding="utf-8")

    prefix = tmp_path / "python-prefix-without-package-data"
    share = tmp_path / "user-base" / "share" / "youngcan-wyckoff-analysis"
    (share / "data").mkdir(parents=True)
    (share / "config" / "profiles").mkdir(parents=True)
    (share / "market_universes").mkdir(parents=True)
    resources = []
    (share / "data" / "stock_list_cache.json").write_text(
        json.dumps([{"code": "000001", "name": "平安银行"}]),
        encoding="utf-8",
    )
    resources.append(share / "data" / "stock_list_cache.json")
    (share / "config" / "profiles" / "a_share_prod.yml").write_text(
        "mainline_engine:\n  max_ai_candidates: 9\nexternal_seeds:\n  enabled: true\n  symbols: ['000001']\n",
        encoding="utf-8",
    )
    resources.append(share / "config" / "profiles" / "a_share_prod.yml")
    (share / "CHANGELOG.md").write_text("# Changelog\n\n## 9.9.9\n\n- packaged\n", encoding="utf-8")
    resources.append(share / "CHANGELOG.md")
    for filename, content in {
        "us.txt": "AAPL.US\n",
        "us_meta.json": json.dumps([{"symbol": "AAPL.US", "name": "Apple"}]),
        "hk.txt": "00700.HK\n",
        "hk_meta.json": json.dumps([{"symbol": "00700.HK", "name": "Tencent"}]),
        "etf_cn.txt": "512480 半导体\n",
    }.items():
        path = share / "market_universes" / filename
        path.write_text(content, encoding="utf-8")
        resources.append(path)

    cwd_universe = tmp_path / "data" / "market_universes"
    cwd_universe.mkdir(parents=True)
    (cwd_universe / "us.txt").write_text("WRONG.US\n", encoding="utf-8")
    (cwd_universe / "us_meta.json").write_text(
        json.dumps([{"symbol": "WRONG.US", "name": "Wrong"}]),
        encoding="utf-8",
    )

    dist_info = site_packages / "youngcan_wyckoff_analysis-0.9.157.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: youngcan-wyckoff-analysis\nVersion: 0.9.157\n",
        encoding="utf-8",
    )
    (dist_info / "RECORD").write_text(
        "\n".join(f"{os.path.relpath(path, site_packages)},," for path in resources),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-c", _INSTALLED_PROBE, str(prefix)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(site_packages)},
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "changelog": True,
        "etf_name": "半导体ETF",
        "external_symbols": ["000001"],
        "hk_symbols": ["00700.HK"],
        "max_ai_candidates": 9,
        "meta_name": "Apple",
        "runtime_path": str(share / "market_universes" / "us.txt"),
        "stock_codes": ["000001"],
        "us_symbols": ["AAPL.US"],
    }


_INSTALLED_PROBE = """
import json
import os
import sys

sys.prefix = sys.argv[1]
for name in tuple(os.environ):
    if name.startswith(("FUNNEL_EXTERNAL_", "FUNNEL_MAINLINE_", "MARKET_FUNNEL_", "WYCKOFF_CONFIG_")):
        os.environ.pop(name)
os.environ.pop("MARKET_UNIVERSE_DIR", None)

from cli.memory import resolve_stock_codes
from cli.tui import WyckoffTUI
from integrations.market_universe import load_hk_symbols, load_us_symbols
from tools.market_universe_meta import load_etf_name_map, load_symbol_name_map
from tools.external_seeds import load_external_seed_config
from tools.mainline_config import load_mainline_engine_config
from workflows.market_funnel_runtime import MARKET_SPECS, market_symbol_path

class Log:
    def __init__(self):
        self.lines = []

    def write(self, value):
        self.lines.append(getattr(value, "plain", str(value)))

log = Log()
WyckoffTUI._show_changelog(object(), log)
us_symbols, _ = load_us_symbols()
hk_symbols, _ = load_hk_symbols()
print(json.dumps({
    "changelog": any("9.9.9" in line for line in log.lines),
    "etf_name": load_etf_name_map().get("512480"),
    "external_symbols": list(load_external_seed_config().symbols),
    "hk_symbols": hk_symbols,
    "max_ai_candidates": load_mainline_engine_config().max_ai_candidates,
    "meta_name": load_symbol_name_map(("us",)).get("AAPL.US"),
    "runtime_path": str(market_symbol_path("us", MARKET_SPECS["us"])),
    "stock_codes": resolve_stock_codes("平安银行"),
    "us_symbols": us_symbols,
}))
"""
