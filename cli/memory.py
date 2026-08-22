"""
Agent 跨会话记忆 — 会话摘要提取 + 记忆注入。
"""

from __future__ import annotations

import logging
import re
from hashlib import sha256
from json import dumps, loads
from typing import Any

from utils.package_resources import runtime_resource

logger = logging.getLogger(__name__)

_SESSION_SUMMARY_PROMPT = """从以下对话中提取值得跨会话记忆的信息（中文）。

只提取这两类：
- [偏好] 用户表达的投资风格、禁忌、操作习惯（如"不追涨"、"只做威科夫形态"）
- [决策] 用户非显而易见的决策逻辑/原因（如"因为板块轮动加速所以缩短持仓周期"）

不要提取：
- 具体买卖了哪只股票（持仓从数据库查询即可）
- 临时操作（加仓、清仓、调仓的事实）
- 当前市场状态（行情每天变）
- 工具调用细节

如果记忆只针对某只股票，必须在内容里保留股票代码或股票名称；如果是全局交易纪律，不要绑定具体股票。
每条一行，前缀标注 [偏好] 或 [决策]。最多输出3条，只写从对话中无法自动推断的洞察，没有则回复"无"。"""

_LAYER_REFRESH_PROMPT = """请基于以下偏好和决策记忆，生成更高层的长期记忆：
- [画像] 用户稳定偏好/风险边界/操作习惯，最多3条
- [剧本] 条件化交易执行模式，最多3条；必须包含适用条件、动作、硬性禁忌或退出条件
只保留可复用交易原则，不要写具体持仓、临时行情、个股即时观点或工具过程，不要编造。"""

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_CJK_RE = re.compile(r"[一-鿿]{2,4}")
_STOPWORDS = frozenset(
    list("的了吗呢啊哦呀吧嘛是不在有我你他它这那都也就要会")
    + [
        "可以",
        "一个",
        "什么",
        "怎么",
        "如何",
        "看看",
        "一下",
        "帮我",
        "请问",
        "能否",
        "可否",
        "这个",
        "那个",
        "我的",
        "你的",
        "现在",
        "我想",
        "想建",
        "建仓",
        "买入",
        "卖出",
        "加仓",
        "减仓",
        "补仓",
        "清仓",
        "持仓",
        "止盈",
        "止损",
    ]
)

_SUMMARY_TYPES = {
    "偏好": "preference",
    "决策": "decision",
}

_LAYER_TYPES = {
    "画像": ("persona", "L3"),
    "剧本": ("playbook", "L2"),
    "交易剧本": ("playbook", "L2"),
    # 兼容旧提示词/旧测试输出；新写入统一归为 playbook。
    "场景": ("playbook", "L2"),
}

DEFAULT_MAX_CHARS_PER_MEMORY = 200
DEFAULT_MAX_TOTAL_RECALL_CHARS = 1200
_RECALL_TRUNCATION_SUFFIX = "…（已截断，可用 wyckoff memory trace 查看来源）"
_LAYER_SOURCE_LIMIT = 30
_LAYER_MIN_ATOMS = 3
_LAYER_VERSION = 3
_LAYER_NEW_ATOMS_THRESHOLD = 3
_SESSION_SUMMARY_VERSION = 3
_PLAYBOOK_MEMORY_TYPES = {"playbook", "scenario"}
_STOCK_NAME_CODE_CACHE: dict[str, str] | None = None


def extract_stock_codes(text: str) -> list[str]:
    return list(dict.fromkeys(_CODE_RE.findall(text)))


def _stock_name_code_map() -> dict[str, str]:
    global _STOCK_NAME_CODE_CACHE
    if _STOCK_NAME_CODE_CACHE is not None:
        return _STOCK_NAME_CODE_CACHE
    path = runtime_resource("data/stock_list_cache.json")
    result: dict[str, str] = {}
    try:
        data = loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            name = str(item.get("name", "")).strip()
            if len(code) == 6 and code.isdigit() and len(name) >= 2:
                result[name] = code
    _STOCK_NAME_CODE_CACHE = result
    return result


def resolve_stock_codes(text: str) -> list[str]:
    codes = extract_stock_codes(text)
    compact = re.sub(r"\s+", "", str(text or ""))
    by_name = sorted(_stock_name_code_map().items(), key=lambda item: len(item[0]), reverse=True)
    for name, code in by_name:
        if name in compact:
            codes.append(code)
    return list(dict.fromkeys(codes))


def _memory_codes(memory: dict) -> set[str]:
    return set(extract_stock_codes(str(memory.get("codes", "") or "")))


def _memory_matches_codes(memory: dict, codes: list[str]) -> bool:
    mem_codes = _memory_codes(memory)
    return not mem_codes or bool(mem_codes & set(codes))


def _extract_keywords(text: str) -> list[str]:
    text = _CODE_RE.sub("", text)
    segments = _CJK_RE.findall(text)
    # 长片段拆成 2-gram 提升召回率
    bigrams: list[str] = []
    for seg in segments:
        if len(seg) <= 2:
            bigrams.append(seg)
        else:
            for i in range(len(seg) - 1):
                bigrams.append(seg[i : i + 2])
    return [s for s in dict.fromkeys(bigrams) if s not in _STOPWORDS][:5]


def _has_tool_calls(messages: list[dict]) -> bool:
    return any(m.get("tool_calls") for m in messages)


def _parse_prefixed_line(line: str, mapping: dict[str, Any]) -> tuple[Any, str] | None:
    stripped = line.strip().lstrip("-* ").strip()
    match = re.match(r"^\[([^\]]+)\]\s*(.+)$", stripped)
    if not match:
        return None
    key = match.group(1).strip()
    content = match.group(2).strip()
    if not content or key not in mapping:
        return None
    return mapping[key], content


def _summary_memories(summary: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for line in summary.strip().splitlines():
        parsed = _parse_prefixed_line(line, _SUMMARY_TYPES)
        if parsed:
            items.append(parsed)
    return items


def _layer_memories(text: str) -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    for line in text.strip().splitlines():
        parsed = _parse_prefixed_line(line, _LAYER_TYPES)
        if parsed:
            (memory_type, level), content = parsed
            items.append((memory_type, level, content))
    return items


def _source_ref(session_id: str) -> str:
    return f"chat_log:{session_id}" if session_id else ""


def _summary_message_content(message: dict) -> str:
    content = str(message.get("content", "") or "")
    if message.get("role") == "tool" and len(content) > 200:
        return content[:200] + "..."
    return content


def _dialog_text_for_summary(messages: list[dict]) -> str:
    lines = []
    for message in messages:
        content = _summary_message_content(message)
        if content:
            lines.append(f"[{message.get('role', '')}] {content}")
    return "\n".join(lines[-40:])


def _session_summary_hash(dialog_text: str) -> str:
    payload = f"v{_SESSION_SUMMARY_VERSION}\n{dialog_text}"
    return sha256(payload.encode("utf-8")).hexdigest()[:16]


def _provider_text(provider: Any, user_text: str, system_prompt: str) -> str:
    chunks = list(provider.chat_stream([{"role": "user", "content": user_text}], [], system_prompt))
    return "".join(c.get("text", "") for c in chunks if c.get("type") == "text_delta")


def _summary_already_processed(source_ref: str, summary_hash: str) -> bool:
    if not source_ref:
        return False
    from integrations.local_db import get_recent_memories

    for memory in get_recent_memories(memory_type="session", limit=50):
        if memory.get("source_ref") != source_ref:
            continue
        metadata = _metadata(memory)
        if metadata.get("summary_hash") == summary_hash:
            return True
    return False


def _mark_summary_processed(source_ref: str, summary_hash: str) -> None:
    if not source_ref:
        return
    from integrations.local_db import save_memory

    save_memory(
        "session",
        f"summary:{summary_hash}",
        source_ref=source_ref,
        metadata={
            "extractor": "session_summary_marker",
            "summary_hash": summary_hash,
            "summary_version": _SESSION_SUMMARY_VERSION,
        },
    )


_DEDUP_PROMPT = """判断"新记忆"是否与以下已有记忆语义重复（含义相同或高度相似即为重复）。
仅回复一行：
- 重复则回复 DUPLICATE:<id>（id 为最匹配的已有记忆编号）
- 不重复则回复 NEW"""


def _normalize_memory_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _bigrams(text: str) -> set[str]:
    normalized = _normalize_memory_text(text)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _find_deterministic_duplicate(content: str, existing: list[dict]) -> int | None:
    normalized = _normalize_memory_text(content)
    grams = _bigrams(content)
    for memory in existing:
        existing_content = str(memory.get("content", ""))
        existing_normalized = _normalize_memory_text(existing_content)
        if normalized == existing_normalized:
            return int(memory["id"])
        if _jaccard(grams, _bigrams(existing_content)) >= 0.92:
            return int(memory["id"])
    return None


def _get_dedup_provider() -> Any | None:
    """获取去重用的 provider：优先 fallback，其次 main。"""
    try:
        from cli.auth import load_default_model_id, load_fallback_model_id, load_model_configs
        from cli.provider_factory import create_provider, provider_config_kwargs

        configs = load_model_configs()
        if not configs:
            return None
        fallback_id = load_fallback_model_id()
        target_id = fallback_id or load_default_model_id()
        cfg = next((c for c in configs if c["id"] == target_id), configs[0])
        provider, err = create_provider(**provider_config_kwargs(cfg))
        return provider if not err else None
    except Exception:
        return None


def _find_duplicate(memory_type: str, content: str, provider: Any | None) -> int | None:
    """用 LLM 判断新记忆是否与同类型已有记忆语义重复，返回重复记忆 id 或 None。"""
    from integrations.local_db import get_recent_memories

    existing = get_recent_memories(memory_type=memory_type, limit=10)
    if not existing:
        return None
    duplicate_id = _find_deterministic_duplicate(content, existing)
    if duplicate_id:
        return duplicate_id
    if provider is None:
        raise RuntimeError("memory dedup provider unavailable")
    lines = [f"#{m['id']}: {m['content']}" for m in existing]
    user_text = "已有记忆:\n" + "\n".join(lines) + f"\n\n新记忆:\n{content}"
    result = _provider_text(provider, user_text, _DEDUP_PROMPT).strip()
    if result == "NEW":
        return None
    match = re.match(r"DUPLICATE[:\s]*#?(\d+)", result)
    if not match:
        raise ValueError("invalid memory dedup response")
    duplicate_id = int(match.group(1))
    if any(m["id"] == duplicate_id for m in existing):
        return duplicate_id
    raise ValueError("dedup duplicate id not found")


def _save_summary_memories(summary: str, source_ref: str, dedup_provider: Any) -> int:
    from integrations.local_db import save_memory

    saved = 0
    for memory_type, content in _summary_memories(summary):
        try:
            dup_id = _find_duplicate(memory_type, content, dedup_provider)
        except Exception:
            logger.debug("memory dedup check failed; skip memory save", exc_info=True)
            continue
        if dup_id:
            logger.debug("memory dedup: '%s' duplicates #%d", content[:50], dup_id)
            continue
        saved += int(
            bool(
                save_memory(
                    memory_type,
                    content,
                    codes=",".join(resolve_stock_codes(content)[:20]),
                    source_ref=source_ref,
                    metadata={"extractor": "session_summary", "summary_version": _SESSION_SUMMARY_VERSION},
                )
            )
        )
    return saved


def _metadata(memory: dict) -> dict:
    raw = memory.get("metadata") or ""
    if not raw:
        return {}
    try:
        data = loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _layer_source(atoms: list[dict]) -> tuple[list[int], str]:
    payload = sorted(
        [{"id": int(m["id"]), "type": m.get("memory_type"), "content": m.get("content")} for m in atoms],
        key=lambda item: item["id"],
    )
    ids = [item["id"] for item in payload]
    source_hash = sha256(dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return ids, source_hash


def _recent_layer_atoms(limit: int = _LAYER_SOURCE_LIMIT) -> list[dict]:
    from integrations.local_db import get_recent_memories

    rows = get_recent_memories(memory_level="L1", limit=limit * 2)
    atoms = [m for m in rows if m.get("memory_type") in {"preference", "decision"}]
    return atoms[:limit]


def _layer_refresh_records() -> list[dict]:
    from integrations.local_db import get_recent_memories

    rows = get_recent_memories(memory_type="persona", limit=5)
    rows.extend(get_recent_memories(memory_type="playbook", limit=20))
    rows.extend(get_recent_memories(memory_type="scenario", limit=20))
    return [
        m
        for m in rows
        if _metadata(m).get("extractor") == "layer_refresh" and _metadata(m).get("layer_version") == _LAYER_VERSION
    ]


def _should_refresh_layers(atoms: list[dict]) -> tuple[bool, list[int], str]:
    if len(atoms) < _LAYER_MIN_ATOMS:
        return False, [], ""
    source_ids, source_hash = _layer_source(atoms)
    records = _layer_refresh_records()
    if any(_metadata(m).get("source_hash") == source_hash for m in records):
        return False, source_ids, source_hash
    last_ids: set[int] = set()
    if records:
        latest = max(records, key=lambda m: str(m.get("created_at", "")))
        last_ids = {int(i) for i in _metadata(latest).get("source_l1_ids", [])}
    new_count = len([i for i in source_ids if i not in last_ids])
    return (not records or new_count >= _LAYER_NEW_ATOMS_THRESHOLD), source_ids, source_hash


def _layer_metadata(source_ids: list[int], source_hash: str) -> dict:
    return {
        "extractor": "layer_refresh",
        "layer_version": _LAYER_VERSION,
        "source_l1_ids": source_ids,
        "source_hash": source_hash,
    }


def refresh_memory_layers(provider: Any) -> int:
    from integrations.local_db import save_memory

    atoms = _recent_layer_atoms()
    should_refresh, source_ids, source_hash = _should_refresh_layers(atoms)
    if not should_refresh:
        return 0
    lines = [f"- #{m.get('id')} [{m.get('memory_type')}] {m.get('content')}" for m in atoms]
    layered = _provider_text(provider, "\n".join(lines), _LAYER_REFRESH_PROMPT)
    metadata = _layer_metadata(source_ids, source_hash)
    saved = 0
    for memory_type, level, content in _layer_memories(layered):
        codes = ",".join(resolve_stock_codes(content)[:20])
        saved += int(bool(save_memory(memory_type, content, codes=codes, memory_level=level, metadata=metadata)))
    return saved


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= len(_RECALL_TRUNCATION_SUFFIX):
        return text[:max_chars]
    return text[: max_chars - len(_RECALL_TRUNCATION_SUFFIX)].rstrip() + _RECALL_TRUNCATION_SUFFIX


def _memory_line(memory: dict, *, max_chars: int = DEFAULT_MAX_CHARS_PER_MEMORY) -> str:
    date_str = str(memory.get("created_at", ""))[:10]
    content = str(memory.get("content", "")).strip()
    content = _truncate_text(content, max_chars)
    source = str(memory.get("source_ref", "")).strip()
    suffix = f" | 源:{source}" if source else ""
    return f"- #{memory.get('id')} [{date_str}] {content}{suffix}"


def _budget_recall_lines(lines: list[str], max_total_chars: int) -> list[str]:
    if max_total_chars <= 0:
        return lines
    budgeted: list[str] = []
    used = 0
    for line in lines:
        separator = 1 if budgeted else 0
        remaining = max_total_chars - used - separator
        if remaining <= 0:
            break
        next_line = _truncate_text(line, remaining) if len(line) > remaining else line
        budgeted.append(next_line)
        used += separator + len(next_line)
        if next_line != line:
            break
    return budgeted


def _wrap_recall_context(lines: list[str]) -> str:
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "<relevant-memories>\n"
        "以下是当前对话召回的相关记忆，不代表当前任务进程，仅作为参考：\n\n"
        f"{body}\n"
        "</relevant-memories>"
    )


def prepend_memory_context(user_text: str, memory_context: str) -> str:
    """Return a current-turn user message with transient recalled memories."""

    if not memory_context.strip():
        return user_text
    return f"{memory_context.strip()}\n\n<current-user-message>\n{user_text}\n</current-user-message>"


def _append_profile_lines(lines: list[str], personas: list[dict], prefs: list[dict], max_chars: int) -> None:
    if not personas and not prefs:
        return
    lines.append("# 用户画像")
    for memory in personas + prefs:
        content = str(memory.get("content", "")).strip()
        if content:
            lines.append(f"- {_truncate_text(content, max_chars)}")


def _append_playbook_lines(lines: list[str], memories: list[dict], max_chars: int) -> None:
    playbooks = [m for m in memories if m.get("memory_type") in _PLAYBOOK_MEMORY_TYPES]
    if not playbooks:
        return
    lines.append("# 交易剧本")
    lines.extend(_memory_line(m, max_chars=max_chars) for m in playbooks[:3])


def _append_atom_lines(lines: list[str], memories: list[dict], max_chars: int) -> None:
    atom_types = {"preference", "decision"}
    atoms = [m for m in memories if m.get("memory_type") in atom_types]
    if not atoms:
        return
    lines.append("# 历史记忆")
    lines.extend(_memory_line(m, max_chars=max_chars) for m in atoms)


def _filter_seen(memories: list[dict], seen_ids: set[int]) -> list[dict]:
    filtered: list[dict] = []
    for memory in memories:
        mid = memory.get("id")
        if mid is not None and int(mid) in seen_ids:
            continue
        filtered.append(memory)
    return filtered


def _filter_by_codes(memories: list[dict], codes: list[str]) -> list[dict]:
    return [m for m in memories if _memory_matches_codes(m, codes)]


def _build_recall_lines(memories: list[dict], personas: list[dict], prefs: list[dict], max_chars: int) -> list[str]:
    lines: list[str] = []
    _append_profile_lines(lines, personas, prefs, max_chars)
    seen_ids = {int(m["id"]) for m in personas + prefs if m.get("id") is not None}
    filtered = _filter_seen(memories, seen_ids)
    _append_playbook_lines(lines, filtered, max_chars)
    _append_atom_lines(lines, filtered, max_chars)
    return lines


def save_session_summary(
    messages: list[dict], provider: Any, *, session_id: str = "", skip_layers: bool = False
) -> None:
    if not messages or len(messages) < 4 or not _has_tool_calls(messages):
        return
    try:
        dialog_text = _dialog_text_for_summary(messages)
        source_ref = _source_ref(session_id)
        summary_hash = _session_summary_hash(dialog_text)
        if _summary_already_processed(source_ref, summary_hash):
            return

        summary = _provider_text(provider, dialog_text, _SESSION_SUMMARY_PROMPT)
        if not summary:
            return
        if len(summary) < 10 and not _summary_memories(summary):
            if summary.strip().startswith("无"):
                _mark_summary_processed(source_ref, summary_hash)
            return

        dedup_provider = _get_dedup_provider() or provider
        saved = _save_summary_memories(summary, source_ref, dedup_provider)
        _mark_summary_processed(source_ref, summary_hash)
        if saved:
            if not skip_layers:
                refresh_memory_layers(provider)
    except Exception:
        logger.debug("save session summary failed", exc_info=True)


def build_memory_context(
    user_message: str,
    *,
    max_chars_per_memory: int = DEFAULT_MAX_CHARS_PER_MEMORY,
    max_total_chars: int = DEFAULT_MAX_TOTAL_RECALL_CHARS,
) -> str:
    try:
        from integrations.local_db import (
            get_recent_memories,
            search_memory_hybrid,
        )

        codes = resolve_stock_codes(user_message)
        keywords = _extract_keywords(user_message)
        from cli.context_archive import archive_recall_lines

        # Hybrid search: FTS5 + 代码 + 关键词 + 时间衰减
        memories = search_memory_hybrid(
            query_text=user_message,
            codes=codes or None,
            keywords=keywords or None,
            limit=8,
            strict_code_scope=True,
        )

        # 高层画像和偏好始终置顶（hybrid search 已包含，但确保完整性）
        personas = _filter_by_codes(get_recent_memories(memory_type="persona", limit=3), codes)[:1]
        prefs = _filter_by_codes(get_recent_memories(memory_type="preference", limit=10), codes)[:5]
        archives = archive_recall_lines(user_message, max_items=2)

        if not memories and not prefs and not personas and not archives:
            return ""

        lines = _build_recall_lines(memories, personas, prefs, max_chars_per_memory)
        from cli.context_layers import default_skill_docs, render_layered_docs

        layered = render_layered_docs(default_skill_docs(), user_message, budget_chars=400)
        if layered:
            lines.append("# 分层技能")
            lines.append(layered)
        if archives:
            lines.append("# 压缩归档")
            lines.extend(archives)
        return _wrap_recall_context(_budget_recall_lines(lines, max_total_chars))
    except Exception:
        return ""
