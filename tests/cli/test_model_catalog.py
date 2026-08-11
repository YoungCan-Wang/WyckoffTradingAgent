"""Tests for OpenRouter model catalog and context-window resolution."""

from __future__ import annotations

import json
import time

import pytest

from cli import model_catalog
from cli.model_registry import infer_model_info
from cli.provider_factory import _resolved_window

_OPENROUTER = "https://openrouter.ai/api/v1"
_PAYLOAD = {
    "data": [
        {"id": "poolside/laguna-xs-2.1", "context_length": 262144},
        {"id": "poolside/laguna-xs-2.1:free", "context_length": 262144},
        {"id": "nvidia/nemotron-3-ultra-550b-a55b:free", "context_length": 1000000},
        {"id": "broken/no-window", "context_length": None},
        {"id": "", "context_length": 1000},
        "not-a-dict",
    ]
}


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Point the cache at tmp and block network so tests never call out."""
    monkeypatch.setattr(model_catalog, "CACHE_PATH", tmp_path / "model_catalog.json")
    monkeypatch.setattr(model_catalog, "fetch_catalog", lambda: {})


def _seed_cache(path, windows: dict[str, int], *, fetched_at: float | None = None) -> None:
    payload = {"fetched_at": time.time() if fetched_at is None else fetched_at, "windows": windows}
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestParseCatalog:
    def test_keeps_valid_rows_only(self):
        parsed = model_catalog._parse_catalog(_PAYLOAD)
        assert parsed["poolside/laguna-xs-2.1:free"] == 262144
        assert parsed["nvidia/nemotron-3-ultra-550b-a55b:free"] == 1000000
        assert "broken/no-window" not in parsed
        assert "" not in parsed

    def test_tolerates_garbage_payload(self):
        assert model_catalog._parse_catalog({}) == {}
        assert model_catalog._parse_catalog({"data": "nope"}) == {}
        assert model_catalog._parse_catalog(None) == {}


class TestCache:
    def test_reads_fresh_cache(self, tmp_path):
        _seed_cache(model_catalog.CACHE_PATH, {"a/b": 1234})
        assert model_catalog.load_catalog() == {"a/b": 1234}

    def test_ignores_expired_cache(self, tmp_path):
        stale = time.time() - model_catalog.CACHE_TTL_SECONDS - 60
        _seed_cache(model_catalog.CACHE_PATH, {"a/b": 1234}, fetched_at=stale)
        # fetch is stubbed to {}, so an expired cache must not leak through.
        assert model_catalog.load_catalog() == {}

    def test_missing_cache_without_fetch_is_empty(self):
        assert model_catalog.load_catalog(allow_fetch=False) == {}


class TestCatalogContextWindow:
    def test_exact_variant_wins_over_base(self, tmp_path):
        # :free 的窗口可能与基础模型不同，必须优先精确匹配。
        _seed_cache(model_catalog.CACHE_PATH, {"m/x": 999_999, "m/x:free": 262_144})
        assert model_catalog.catalog_context_window("m/x:free") == 262_144

    def test_falls_back_to_base_model(self, tmp_path):
        _seed_cache(model_catalog.CACHE_PATH, {"m/x": 262_144})
        assert model_catalog.catalog_context_window("m/x:free") == 262_144

    def test_unknown_returns_none(self, tmp_path):
        _seed_cache(model_catalog.CACHE_PATH, {"m/x": 1})
        assert model_catalog.catalog_context_window("nope/zzz") is None
        assert model_catalog.catalog_context_window("") is None


class TestLooksLikeOpenrouter:
    def test_detects_host(self):
        assert model_catalog.looks_like_openrouter(_OPENROUTER)
        assert not model_catalog.looks_like_openrouter("https://api.deepseek.com")
        assert not model_catalog.looks_like_openrouter("")


class TestWindowPrecedence:
    """config > catalog > name pattern."""

    def test_config_wins(self, tmp_path):
        _seed_cache(model_catalog.CACHE_PATH, {"poolside/laguna-xs-2.1:free": 262_144})
        info = infer_model_info(
            {
                "provider_name": "openai",
                "model": "poolside/laguna-xs-2.1:free",
                "base_url": _OPENROUTER,
                "context_window": 4096,
            }
        )
        assert (info.context_window, info.window_source) == (4096, "config")

    def test_catalog_used_for_openrouter(self, tmp_path):
        _seed_cache(model_catalog.CACHE_PATH, {"poolside/laguna-xs-2.1:free": 262_144})
        info = infer_model_info(
            {"provider_name": "openai", "model": "poolside/laguna-xs-2.1:free", "base_url": _OPENROUTER}
        )
        assert (info.context_window, info.window_source) == (262_144, "catalog")

    def test_non_openrouter_stays_on_patterns(self, tmp_path):
        # 其它网关的 id 命名空间不同，用 OpenRouter 目录会误配。
        _seed_cache(model_catalog.CACHE_PATH, {"poolside/laguna-xs-2.1:free": 262_144})
        info = infer_model_info(
            {"provider_name": "openai", "model": "poolside/laguna-xs-2.1:free", "base_url": "https://api.acme.com"}
        )
        assert (info.context_window, info.window_source) == (64_000, "inferred")

    def test_known_pattern_still_wins_without_catalog(self):
        info = infer_model_info({"provider_name": "claude", "model": "claude-sonnet-4-20260514", "base_url": ""})
        assert (info.context_window, info.window_source) == (200_000, "inferred")


class TestResolvedWindowForProvider:
    def test_prefers_config(self, tmp_path):
        _seed_cache(model_catalog.CACHE_PATH, {"m/x": 262_144})
        assert _resolved_window(8192, "m/x", _OPENROUTER) == 8192

    def test_uses_catalog_when_config_missing(self, tmp_path):
        _seed_cache(model_catalog.CACHE_PATH, {"m/x": 262_144})
        assert _resolved_window(None, "m/x", _OPENROUTER) == 262_144

    def test_zero_for_unknown_so_compaction_falls_back(self, tmp_path):
        # 返回 0 表示「不设置」，压缩层继续走 resolve_context_window 的名称推断。
        _seed_cache(model_catalog.CACHE_PATH, {"m/x": 262_144})
        assert _resolved_window(None, "other/y", _OPENROUTER) == 0
        assert _resolved_window(None, "m/x", "https://api.acme.com") == 0

    def test_bad_config_value_does_not_raise(self, tmp_path):
        _seed_cache(model_catalog.CACHE_PATH, {"m/x": 262_144})
        assert _resolved_window("garbage", "m/x", _OPENROUTER) == 262_144
