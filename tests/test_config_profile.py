from __future__ import annotations

from dataclasses import fields

import pytest
import yaml

from core.wyckoff_engine import FunnelConfig
from tools.external_seeds import load_external_seed_config
from tools.mainline_config import load_mainline_engine_config
from utils import config_profile
from workflows.funnel_config_overrides import funnel_cfg_overrides_from_env


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    import os

    for key in os.environ:
        if key.startswith(("WYCKOFF_CONFIG_", "FUNNEL_MAINLINE_", "FUNNEL_EXTERNAL_", "FUNNEL_CFG_")):
            monkeypatch.delenv(key)
    monkeypatch.delenv("FUNNEL_EXTRA_SYMBOLS", raising=False)


def test_explicit_path_wins_over_named_profile(monkeypatch, tmp_path):
    path = tmp_path / "private.yml"
    path.write_text("mainline_engine:\n  max_ai_candidates: 7\n")
    monkeypatch.setenv("WYCKOFF_CONFIG_PATH", str(path))
    monkeypatch.setenv("WYCKOFF_CONFIG_PROFILE", "missing")
    assert load_mainline_engine_config().max_ai_candidates == 7


@pytest.mark.parametrize("profile", ["", "a_share_prod", "custom"])
def test_named_profile_uses_packaged_resources(monkeypatch, tmp_path, profile):
    path = tmp_path / "profile.yml"
    path.write_text("mainline_engine:\n  max_ai_candidates: 6\n")
    seen = []

    def resolve(name):
        seen.append(name)
        return path

    monkeypatch.setattr(config_profile, "runtime_resource", resolve)
    monkeypatch.setenv("WYCKOFF_CONFIG_PROFILE", profile)
    assert load_mainline_engine_config().max_ai_candidates == 6
    assert seen == [f"config/profiles/{profile or 'a_share_prod'}.yml"]


def test_profile_accepts_path_and_tilde(monkeypatch, tmp_path):
    path = tmp_path / "custom.yml"
    path.write_text("external_seeds:\n  enabled: true\n  symbols: ['000001']\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WYCKOFF_CONFIG_PROFILE", "~/custom.yml")
    assert load_external_seed_config().symbols == ("000001",)


@pytest.mark.parametrize("text", ["", "[]", "mainline_engine: []", "mainline_engine: null"])
def test_missing_or_wrong_section_preserves_defaults(monkeypatch, tmp_path, text):
    path = tmp_path / "profile.yml"
    path.write_text(text)
    monkeypatch.setenv("WYCKOFF_CONFIG_PATH", str(path))
    assert load_mainline_engine_config().max_ai_candidates == 3
    assert load_external_seed_config().enabled is False


def test_missing_file_preserves_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("WYCKOFF_CONFIG_PATH", str(tmp_path / "missing.yml"))
    assert config_profile.load_profile_section("mainline_engine") == {}


def test_malformed_yaml_still_raises(monkeypatch, tmp_path):
    path = tmp_path / "profile.yml"
    path.write_text("mainline_engine: [")
    monkeypatch.setenv("WYCKOFF_CONFIG_PATH", str(path))
    with pytest.raises(yaml.YAMLError):
        load_mainline_engine_config()


def test_both_loaders_keep_environment_precedence(monkeypatch, tmp_path):
    path = tmp_path / "profile.yml"
    path.write_text(
        "mainline_engine:\n  enabled: true\n  max_ai_candidates: 7\n"
        "  min_theme_score: 0.8\n  allow_l4_bypass: false\n"
        "external_seeds:\n  enabled: true\n  symbols: ['000001']\n  max_symbols: 8\n"
    )
    monkeypatch.setenv("WYCKOFF_CONFIG_PATH", str(path))
    monkeypatch.setenv("FUNNEL_MAINLINE_ENGINE_ENABLED", "0")
    monkeypatch.setenv("FUNNEL_MAINLINE_MAX_AI_CANDIDATES", "2")
    monkeypatch.setenv("FUNNEL_MAINLINE_MIN_THEME_SCORE", "0.6")
    monkeypatch.setenv("FUNNEL_EXTERNAL_SEED_MAX", "1")
    monkeypatch.setenv("FUNNEL_EXTERNAL_SEED_SYMBOLS", "000002")
    mainline = load_mainline_engine_config()
    external = load_external_seed_config()
    assert mainline.enabled is False
    assert mainline.max_ai_candidates == 2
    assert mainline.min_theme_score == 0.6
    assert mainline.allow_l4_bypass is False
    assert external.enabled is True
    assert external.symbols == ("000001",)


@pytest.mark.parametrize("raw,expected", [(None, True), (False, False), ("0", False), ("yes", True)])
def test_bool_parser_preserves_existing_semantics(raw, expected):
    assert config_profile.bool_value(raw, True) is expected


@pytest.mark.parametrize("raw,expected", [(None, 3), ("bad", 3), ("2.8", 2), (-1, 0)])
def test_integer_parser_preserves_clamping(raw, expected):
    assert config_profile.int_value(raw, 3) == expected


def test_environment_alias_uses_first_nonempty_value(monkeypatch):
    monkeypatch.setenv("FIRST", " ")
    monkeypatch.setenv("SECOND", " 0 ")
    assert config_profile.env_value("FIRST", "SECOND") == "0"


def test_removed_inert_fields_and_protected_switch_are_not_overrides(monkeypatch):
    for key in ("EVR_LOOKBACK", "MARKUP_RS_POSITIVE_MIN", "ENABLE_EVR_TRIGGER"):
        monkeypatch.setenv(f"FUNNEL_CFG_{key}", "0")
    monkeypatch.setenv("FUNNEL_CFG_EVR_VOL_RATIO", "1.9")
    assert funnel_cfg_overrides_from_env() == {"evr_vol_ratio": 1.9}
    names = {field.name for field in fields(FunnelConfig)}
    assert "evr_lookback" not in names
    assert "markup_rs_positive_min" not in names
    assert FunnelConfig().enable_evr_trigger is True


def test_env_example_contains_only_supported_funnel_overrides():
    from pathlib import Path

    text = (Path(__file__).parents[1] / ".env.example").read_text()
    keys = {line.split("=", 1)[0] for line in text.splitlines() if line.startswith("FUNNEL_CFG_")}
    allowed = {f"FUNNEL_CFG_{field.name.upper()}" for field in fields(FunnelConfig)}
    assert keys <= allowed
    assert "FUNNEL_CFG_ENABLE_EVR_TRIGGER" not in keys
