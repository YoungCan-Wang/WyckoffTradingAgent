"""Shared public profile loading and scalar parsing for candidate tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from utils.env import parse_bool
from utils.package_resources import runtime_resource


def load_profile_section(name: str) -> dict[str, Any]:
    raw_path = os.getenv("WYCKOFF_CONFIG_PATH", "").strip()
    profile = os.getenv("WYCKOFF_CONFIG_PROFILE", "a_share_prod").strip() or "a_share_prod"
    if raw_path:
        path = Path(raw_path).expanduser()
    elif "/" in profile or profile.endswith((".yml", ".yaml")):
        path = Path(profile).expanduser()
    else:
        path = runtime_resource(f"config/profiles/{profile}.yml")
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = data.get(name) if isinstance(data, dict) else {}
    return section if isinstance(section, dict) else {}


def env_value(*names: str) -> str | None:
    for name in names:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def bool_value(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return parse_bool(str(raw))


def int_value(raw: Any, default: int, *, minimum: int = 0) -> int:
    try:
        return max(int(float(raw)), minimum)
    except (TypeError, ValueError):
        return default
