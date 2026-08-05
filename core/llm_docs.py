"""Load versioned, advisory context for trading LLM workflows."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LLM_DOC_ROOT = Path(__file__).resolve().parents[1] / "llmdoc"
DEFAULT_CONTEXT_MAX_CHARS = 3_000


def _load_index(root: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads((root / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        logger.warning("LLM doc index unavailable: %s", root / "index.json", exc_info=True)
        return []
    documents = payload.get("documents", []) if isinstance(payload, dict) else []
    return [item for item in documents if isinstance(item, dict)]


def _matches(entry: dict[str, Any], scope: str, symbols: set[str], as_of: date) -> bool:
    if entry.get("enabled", True) is not True:
        return False
    scopes = _string_set(entry.get("scopes"))
    if scope not in scopes:
        return False
    entry_symbols = _string_set(entry.get("symbols"))
    if entry_symbols and not entry_symbols.intersection(symbols):
        return False
    if not _date_boundary_allows(entry.get("effective_from"), as_of, is_start=True):
        return False
    return _date_boundary_allows(entry.get("expires_on"), as_of, is_start=False)


def _string_set(value: object) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _date_boundary_allows(raw: object, as_of: date, *, is_start: bool) -> bool:
    boundary = str(raw or "").strip()
    if not boundary:
        return True
    try:
        parsed = date.fromisoformat(boundary)
    except ValueError:
        logger.warning("Ignore LLM doc with invalid date boundary: %s", boundary)
        return False
    return as_of >= parsed if is_start else as_of <= parsed


def _priority(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("priority", 0))
    except (TypeError, ValueError):
        return 0


def _read_document(root: Path, entry: dict[str, Any]) -> tuple[str, str] | None:
    doc_id = str(entry.get("id") or "").strip()
    relative_path = str(entry.get("path") or "").strip()
    if not doc_id or not relative_path:
        return None
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix.lower() != ".md":
        logger.warning("Reject unsafe LLM doc path: %s", relative_path)
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("LLM doc unavailable: %s", path, exc_info=True)
        return None
    return (doc_id, content) if content else None


def build_llm_doc_context(
    scope: str,
    *,
    symbols: list[str] | tuple[str, ...] | set[str] = (),
    as_of: date | None = None,
    max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
    root: Path | None = None,
) -> str:
    """Return bounded advisory documents relevant to one workflow invocation."""
    doc_root = (root or DEFAULT_LLM_DOC_ROOT).resolve()
    current_symbols = {str(symbol).strip() for symbol in symbols if str(symbol).strip()}
    entries = sorted(_load_index(doc_root), key=_priority, reverse=True)
    selected = [entry for entry in entries if _matches(entry, scope, current_symbols, as_of or date.today())]
    blocks: list[str] = []
    for entry in selected:
        document = _read_document(doc_root, entry)
        if document:
            blocks.append(f'<document id="{document[0]}">\n{document[1]}\n</document>')
    if not blocks:
        return ""
    header = (
        "[LLM决策注释-咨询性]\n"
        "以下内容经过版本控制，只用于提醒模型检查遗漏风险；不得覆盖实时数据、硬止损、市场闸门、OMS或输出格式。\n"
    )
    return (header + "\n".join(blocks))[: max(int(max_chars), 0)]
