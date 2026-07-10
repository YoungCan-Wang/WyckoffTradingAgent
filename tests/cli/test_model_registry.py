from __future__ import annotations

from pathlib import Path

from cli.model_registry import estimate_cost_usd, infer_model_info, summarize_model_usage


def _init_tmp_db(monkeypatch, tmp_path: Path):
    import integrations.local_db as local_db

    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "wyckoff.db")
    local_db.init_db()
    return local_db


def _close_tmp_db(local_db) -> None:
    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None


def test_infer_model_info_exposes_context_and_reasoning():
    info = infer_model_info({"provider_name": "gemini", "model": "gemini-2.5-pro"})

    assert info.context_window == 1_000_000
    assert info.supports_reasoning is True
    assert "medium" in info.thinking_levels


def test_infer_minimax_m3_metadata():
    info = infer_model_info({"provider_name": "minimax", "model": "MiniMax-M3"})

    assert info.context_window == 1_000_000
    assert info.supports_reasoning is True
    assert info.thinking_levels == ("off", "adaptive")


def test_estimate_cost_uses_configured_prices():
    info = infer_model_info(
        {
            "provider_name": "openai",
            "model": "gpt-4o",
            "input_cost_per_1m": 2.5,
            "output_cost_per_1m": 10,
        }
    )

    assert estimate_cost_usd(info, tokens_in=1_000_000, tokens_out=500_000) == 7.5


def test_summarize_model_usage_estimates_known_config_cost(tmp_path: Path, monkeypatch):
    local_db = _init_tmp_db(monkeypatch, tmp_path)
    try:
        local_db.save_chat_log(
            "s1",
            "assistant",
            "ok",
            provider="openai",
            model="gpt-4o",
            tokens_in=1000,
            tokens_out=500,
            elapsed_s=1.2,
        )

        rows = summarize_model_usage(
            days=7,
            configs=[
                {
                    "provider_name": "openai",
                    "model": "gpt-4o",
                    "input_cost_per_1m": 2,
                    "output_cost_per_1m": 4,
                }
            ],
        )

        assert len(rows) == 1
        assert rows[0].tokens_in == 1000
        assert rows[0].tokens_out == 500
        assert rows[0].estimated_cost == 0.004
    finally:
        _close_tmp_db(local_db)
