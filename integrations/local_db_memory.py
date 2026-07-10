"""Compatibility exports for local SQLite agent memory persistence."""

from integrations.local_db import (
    get_db,
    get_memory_by_id,
    get_recent_memories,
    prune_agent_memory_for_connection,
    prune_memories,
    save_memory,
    search_memory,
    search_memory_by_keywords,
    search_memory_fts,
    search_memory_hybrid,
)

__all__ = [
    "get_db",
    "save_memory",
    "get_memory_by_id",
    "search_memory",
    "get_recent_memories",
    "search_memory_by_keywords",
    "search_memory_fts",
    "search_memory_hybrid",
    "prune_agent_memory_for_connection",
    "prune_memories",
]
