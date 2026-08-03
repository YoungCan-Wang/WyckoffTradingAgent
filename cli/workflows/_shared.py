"""Shared helpers used by multiple workflow modules."""

from __future__ import annotations

import json
import re
from typing import Any

# ── Marker tuples (duplicated between model_router.py and planner.py) ──

PORTFOLIO_REVIEW_SUBJECT_MARKERS = ("持仓", "仓位", "组合")
PORTFOLIO_REVIEW_STRONG_MARKERS = ("复盘", "体检", "诊断", "总结", "去留", "攻防", "策略")
PORTFOLIO_REVIEW_CONTEXT_MARKERS = ("大盘", "市场", "水温", "盘面", "环境", "今天", "明天", "风险", "建议")
PORTFOLIO_REVIEW_ACTION_MARKERS = (
    *PORTFOLIO_REVIEW_STRONG_MARKERS,
    "风险",
    "处理",
    "要处理",
    "怎么办",
    "怎么看",
    "怎么样",
    "调整",
    "减仓",
    "止损",
    "加仓",
    "下一步",
)
PORTFOLIO_REVIEW_EXPLAIN_MARKERS = ("是什么意思", "啥意思", "概念", "解释")
PORTFOLIO_REVIEW_PERSONAL_MARKERS = ("我", "当前", "今天", "明天")

STOCK_STYLE_MARKERS = (
    "强势",
    "趋势",
    "低吸",
    "右侧",
    "左侧",
    "稳健",
    "最强",
    "领涨",
    "龙头",
    "短线",
    "起爆",
    "弹性",
    "刚启动",
    "初启动",
    "低位",
    "不追高",
    "别追高",
    "不追涨",
    "性价比",
    "蓝筹",
    "白马",
    "成交活跃",
    "流动性好",
    "高流动性",
)
STOCK_STYLE_TARGETS = ("票", "标的", "候选", "蓝筹", "白马")

# ── Text helpers ──


def compact_text(value: Any) -> str:
    """Strip punctuation / whitespace for keyword-matching on user input."""
    return re.sub(r"[\s。！!,.，、？?]+", "", str(value or "").lower())


def recent_dialogue_context(
    messages: list[dict[str, Any]] | None,
    current_user_text: str,
    *,
    max_messages: int,
    max_chars: int,
) -> str:
    """Render the tail of the dialogue for routing / planning prompts.

    Router 和 planner 必须看同一份历史。两边各写一份实现，就会出现路由知道「继续录入」是
    承接上一轮、而 planner 完全不知道上一轮说了什么的信息量断层。
    """
    if not messages:
        return ""
    lines: list[str] = []
    skipped_current = False
    for message in reversed(messages):
        role = str(message.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        text = dialogue_message_text(message)
        if not text:
            continue
        if not skipped_current and role == "user" and text == current_user_text:
            skipped_current = True
            continue
        label = "用户" if role == "user" else "助手"
        clipped = text[:max_chars] + ("..." if len(text) > max_chars else "")
        lines.append(f"{label}: {clipped}")
        if len(lines) >= max_messages:
            break
    return "\n".join(reversed(lines))


def dialogue_message_text(message: dict[str, Any]) -> str:
    """Flatten one message to a single whitespace-normalised line."""
    raw = message.get("_raw_content") or message.get("content") or ""
    if isinstance(raw, list):
        raw = " ".join(str(item) for item in raw)
    return " ".join(str(raw).split())


def collect_stream_text(chunks: Any) -> str:
    """Concatenate text_delta chunks from a streaming provider response."""
    parts: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if chunk.get("type") == "tool_calls":
            return ""
        if chunk.get("type") == "text_delta":
            parts.append(str(chunk.get("text", "")))
    return "".join(parts).strip()


def provider_chat_response(
    provider: Any, messages: list[dict[str, Any]], system_prompt: str, *, stream_fallback_flag: str
) -> dict[str, Any] | None:
    """Call a provider for a one-shot JSON decision, falling back to streaming if needed."""
    if hasattr(provider, "chat"):
        try:
            return provider.chat(messages, [], system_prompt)
        except NotImplementedError:
            if not getattr(provider, stream_fallback_flag, False):
                return None
    if not hasattr(provider, "chat_stream"):
        return None
    text = collect_stream_text(provider.chat_stream(messages, [], system_prompt))
    return {"type": "text", "text": text} if text else None


# ── JSON helpers ──


def loads_json(text: str, *, error_label: str = "decision") -> dict[str, Any]:
    """Parse JSON from raw LLM output, tolerating markdown fences and noise."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError(f"{error_label} must be an object")
    return payload


# ── Confidence parsing (robust version from model_router.py) ──


def parse_confidence(value: Any) -> float | None:
    """Parse a confidence/score/probability value, normalising to 0-1."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        text = value.strip()
        multiplier = 0.01 if text.endswith("%") else 1.0
        value = text.rstrip("%").strip()
    else:
        multiplier = 1.0
    try:
        confidence = float(value) * multiplier
    except (TypeError, ValueError):
        return None
    if confidence > 1.0:
        confidence /= 100.0
    return round(max(0.0, min(confidence, 1.0)), 4)


def decision_confidence(payload: dict[str, Any]) -> float:
    """Extract confidence from a model decision payload."""
    for key in ("confidence", "score", "probability", "prob"):
        confidence = parse_confidence(payload.get(key))
        if confidence is not None:
            return confidence
    return 0.0


# ── Marker-based detection helpers ──


def has_stock_style_target(text: str) -> bool:
    return any(marker in text for marker in STOCK_STYLE_MARKERS) and any(
        marker in text for marker in STOCK_STYLE_TARGETS
    )


def looks_like_portfolio_review(text: str) -> bool:
    compacted = compact_text(text)
    if not compacted:
        return False
    has_subject = any(marker in compacted for marker in PORTFOLIO_REVIEW_SUBJECT_MARKERS)
    if not has_subject or _looks_like_portfolio_term_question(compacted):
        return False
    return any(marker in compacted for marker in PORTFOLIO_REVIEW_ACTION_MARKERS)


def _looks_like_portfolio_term_question(text: str) -> bool:
    has_explain_marker = any(marker in text for marker in PORTFOLIO_REVIEW_EXPLAIN_MARKERS)
    has_personal_marker = any(marker in text for marker in PORTFOLIO_REVIEW_PERSONAL_MARKERS)
    return has_explain_marker and not has_personal_marker
