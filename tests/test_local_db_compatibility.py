from __future__ import annotations

from importlib import import_module

import pytest

from integrations import local_db

COMPATIBILITY_EXPORTS = [
    pytest.param(
        "integrations.local_db_chat",
        (
            ("background_task_result_summary", "background_task_result_summary"),
            ("get_db", "get_db"),
            ("save_chat_log", "save_chat_log"),
            ("load_chat_logs", "load_chat_logs"),
            ("save_background_task_result", "save_background_task_result"),
            ("load_background_task_results", "load_background_task_results"),
            ("load_background_task_result", "load_background_task_result"),
            ("get_session_preview", "get_session_preview"),
        ),
        id="chat",
    ),
    pytest.param(
        "integrations.local_db_maintenance",
        (
            ("get_db", "get_db"),
            ("prune_agent_memory_for_connection", "prune_agent_memory_for_connection"),
            ("cleanup_old_records", "cleanup_old_records"),
        ),
        id="maintenance",
    ),
    pytest.param(
        "integrations.local_db_memory",
        (
            ("get_db", "get_db"),
            ("save_memory", "save_memory"),
            ("get_memory_by_id", "get_memory_by_id"),
            ("search_memory", "search_memory"),
            ("get_recent_memories", "get_recent_memories"),
            ("search_memory_by_keywords", "search_memory_by_keywords"),
            ("search_memory_fts", "search_memory_fts"),
            ("search_memory_hybrid", "search_memory_hybrid"),
            ("prune_agent_memory_for_connection", "prune_agent_memory_for_connection"),
            ("prune_memories", "prune_memories"),
        ),
        id="memory",
    ),
    pytest.param(
        "integrations.local_db_portfolio",
        (
            ("get_db", "get_db"),
            ("save_portfolio", "save_portfolio"),
            ("load_portfolio", "load_portfolio"),
            ("upsert_local_position", "upsert_local_position"),
            ("delete_local_position", "delete_local_position"),
            ("update_local_free_cash", "update_local_free_cash"),
        ),
        id="portfolio",
    ),
    pytest.param(
        "integrations.local_db_sessions",
        (
            ("get_db", "get_db"),
            ("delete_chat_session", "delete_chat_session"),
            ("list_chat_sessions", "list_chat_sessions"),
        ),
        id="sessions",
    ),
    pytest.param(
        "integrations.local_db_sync_meta",
        (
            ("get_db", "get_db"),
            ("update_sync_meta", "update_sync_meta"),
            ("get_sync_meta", "get_sync_meta"),
            ("needs_sync", "needs_sync"),
        ),
        id="sync_meta",
    ),
    pytest.param(
        "integrations.local_db_tail_buy",
        (
            ("get_db", "get_db"),
            ("save_tail_buy_results", None),
            ("load_tail_buy_history", "load_tail_buy_history"),
        ),
        id="tail_buy",
    ),
]


@pytest.mark.parametrize(("module_name", "exports"), COMPATIBILITY_EXPORTS)
def test_split_module_preserves_callable_exports(module_name, exports):
    module = import_module(module_name)

    for exported_name, canonical_name in exports:
        exported = getattr(module, exported_name)
        assert callable(exported)
        if canonical_name is not None:
            assert exported is getattr(local_db, canonical_name)

    assert tuple(module.__all__) == tuple(exported_name for exported_name, _ in exports)


def test_tail_buy_empty_save_does_not_open_database(monkeypatch):
    tail_buy = import_module("integrations.local_db_tail_buy")

    def fail_get_db():
        raise AssertionError("empty tail-buy save must not access the database")

    monkeypatch.setattr(tail_buy, "get_db", fail_get_db)
    monkeypatch.setattr(local_db, "get_db", fail_get_db)

    assert tail_buy.save_tail_buy_results([]) == 0


def test_prune_public_alias_preserves_private_patch_target():
    assert local_db.prune_agent_memory_for_connection is local_db._prune_agent_memory
