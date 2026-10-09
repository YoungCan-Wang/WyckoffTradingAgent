"""Load mainline engine config from profile and environment."""

from __future__ import annotations

from typing import Any

from core.mainline_engine import MainlineEngineConfig
from core.theme_radar import normalize_theme_name
from integrations.fetch_a_share_csv import normalize_symbols
from utils.config_profile import bool_value as _bool_value
from utils.config_profile import env_value as _env_value
from utils.config_profile import int_value as _int_value
from utils.config_profile import load_profile_section


def load_mainline_engine_config() -> MainlineEngineConfig:
    section = load_profile_section("mainline_engine")
    return MainlineEngineConfig(
        enabled=_bool_value(_env_value("FUNNEL_MAINLINE_ENGINE_ENABLED") or section.get("enabled"), True),
        max_ai_candidates=_int_value(
            _env_value("FUNNEL_MAINLINE_MAX_AI_CANDIDATES") or section.get("max_ai_candidates"), 3
        ),
        min_theme_score=_float_value(
            _env_value("FUNNEL_MAINLINE_MIN_THEME_SCORE") or section.get("min_theme_score"), 0.55
        ),
        min_stock_score=_float_value(
            _env_value("FUNNEL_MAINLINE_MIN_STOCK_SCORE") or section.get("min_stock_score"), 0.60
        ),
        min_timing_score=_float_value(
            _env_value("FUNNEL_MAINLINE_MIN_TIMING_SCORE") or section.get("min_timing_score"), 0.55
        ),
        allow_l2_bypass=_bool_value(
            _env_value("FUNNEL_MAINLINE_ALLOW_L2_BYPASS") or section.get("allow_l2_bypass"), True
        ),
        allow_l4_bypass=_bool_value(
            _env_value("FUNNEL_MAINLINE_ALLOW_L4_BYPASS") or section.get("allow_l4_bypass"), False
        ),
        max_candidates_per_theme=_int_value(section.get("max_candidates_per_theme"), 8),
        themes=_themes(section.get("themes")),
        core_basket=_core_basket(section.get("core_basket")),
    )


def _themes(raw: Any) -> tuple[str, ...]:
    items = raw if isinstance(raw, list | tuple) else []
    out = [normalize_theme_name(str(item)) for item in items]
    return tuple(dict.fromkeys(theme for theme in out if theme))


def _core_basket(raw: Any) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(raw, list | tuple):
        return ()
    rows: list[tuple[str, str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            codes = normalize_symbols([str(item.get("code") or item.get("symbol") or "")])
            theme = normalize_theme_name(str(item.get("theme") or ""))
            name = str(item.get("name") or (codes[0] if codes else "")).strip()
            if codes and theme:
                rows.append((codes[0], name, theme))
    return tuple(rows)


def _float_value(raw: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(float(raw), minimum)
    except (TypeError, ValueError):
        return default
