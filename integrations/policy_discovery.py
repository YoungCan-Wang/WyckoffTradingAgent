"""Real-time policy & theme self-iteration powered by DeepSeek and live market event streams."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from core.concept_filters import is_actionable_theme_name
from core.theme_radar import THEME_ALIASES, merge_theme_aliases, normalize_theme_name
from integrations.llm_client import call_llm, get_provider_credentials
from utils.trading_clock import cn_today

logger = logging.getLogger(__name__)

DEFAULT_POLICY_CACHE_PATH = Path("data/dynamic_policy_themes_cache.json")
DEFAULT_POLICY_TTL_DAYS = 14
EASTMONEY_724_URL = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
_CN_TZ = ZoneInfo("Asia/Shanghai")
_STATIC_ALIAS_OWNERS = {
    alias.casefold(): theme for theme, keys in THEME_ALIASES.items() for alias in (theme, *keys) if alias
}

_SYSTEM_PROMPT = (
    "你是A股威科夫量化系统的政策与资金抱团解构引擎。"
    "请根据输入的最新监管政策、央行公告、财经快讯与盘面热点，提炼出结构化JSON（严禁输出JSON以外的解释）：\n"
    "{\n"
    '  "dynamic_aliases": {"标准主题名": ["新政策词1", "新概念词2"]},\n'
    '  "boost_themes": [{"theme": "标准主题名", "weight": 0.80, "title": "政策摘要", "source": "来源"}],\n'
    '  "squeeze_themes": [{"theme": "受敲打/去杠杆主题名", "penalty": 0.65, "title": "监管敲打摘要", "source": "来源"}]\n'
    "}\n"
    "规则：1. 标准主题名必须严格从系统已知标准主题列表中选择，同义词长度不少于3个字符；"
    "2. 区分真金白银落地（weight 0.75~0.85）与一般表态（0.50~0.70）；"
    "3. 监管层点名提示风险、过高杠杆、产能过剩或挤出效应的板块必须归入 squeeze_themes（penalty 0.45~0.75）。"
)


def resolve_policy_cache_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    env_path = os.getenv("POLICY_THEME_CACHE_PATH", "").strip()
    return Path(env_path) if env_path else DEFAULT_POLICY_CACHE_PATH


def load_dynamic_policy_themes(
    *,
    trade_date: str | None = None,
    path: Path | str | None = None,
) -> dict[str, Any]:
    target = resolve_policy_cache_path(path)
    if not target.exists():
        return _empty_policy(trade_date)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("load dynamic policy cache failed: %s", exc)
        return _empty_policy(trade_date)
    if _is_expired(payload, trade_date):
        return _empty_policy(trade_date)
    return _sanitize_policy_payload(payload, trade_date=trade_date)


def save_dynamic_policy_themes(
    payload: dict[str, Any],
    *,
    path: Path | str | None = None,
) -> Path:
    target = resolve_policy_cache_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    sanitized = _sanitize_policy_payload(payload, trade_date=str(payload.get("trade_date") or ""))
    target.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def collect_live_policy_headlines(
    *,
    ths_hot_events: dict[str, Any] | None = None,
    extra_headlines: list[dict[str, Any]] | None = None,
    timeout: int = 6,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    items.extend(extra_headlines or [])
    items.extend(_headlines_from_ths_events(ths_hot_events))
    items.extend(_fetch_eastmoney_telegraph(timeout=timeout))
    return _dedupe_headlines(items)[:40]


def analyze_policy_headlines_with_deepseek(
    headlines: list[dict[str, Any]],
    *,
    trade_date: str,
) -> dict[str, Any]:
    api_key, model, base_url = get_provider_credentials("deepseek")
    if not headlines or not api_key:
        return _empty_policy(trade_date)
    canonical = "、".join(THEME_ALIASES.keys())
    lines = [
        f"- [{_clean_text(h.get('source', '快讯'), 16)}] "
        f"{_clean_text(h.get('title', ''), 80)} {_clean_text(h.get('summary', ''), 120)}".strip()
        for h in headlines[:30]
        if _clean_text(h.get("title") or h.get("summary") or "", 120)
    ]
    user_prompt = f"交易日：{trade_date}\n当前系统已知标准主题：{canonical}\n\n最新政策与市场事件：\n" + "\n".join(
        lines
    )
    fast_model = os.getenv("DEEPSEEK_FAST_MODEL", "").strip() or model or "deepseek-v4-flash"
    try:
        raw = call_llm(
            provider="deepseek",
            model=fast_model,
            api_key=api_key,
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_prompt,
            base_url=base_url,
            timeout=45,
            max_output_tokens=2048,
        )
        parsed = _extract_json_object(raw)
        return _sanitize_policy_payload(parsed, trade_date=trade_date)
    except Exception as exc:
        logger.warning("deepseek policy discovery failed: %s", exc)
        return _empty_policy(trade_date)


def refresh_dynamic_policy_themes(
    *,
    trade_date: str | None = None,
    ths_hot_events: dict[str, Any] | None = None,
    extra_headlines: list[dict[str, Any]] | None = None,
    cluster_watch: list[dict[str, Any]] | None = None,
    discovered_policy: dict[str, Any] | None = None,
    path: Path | str | None = None,
) -> dict[str, Any]:
    effective_date = str(trade_date or cn_today().isoformat()).strip()
    existing = load_dynamic_policy_themes(trade_date=effective_date, path=path)
    if discovered_policy is None:
        headlines = collect_live_policy_headlines(ths_hot_events=ths_hot_events, extra_headlines=extra_headlines)
        discovered = analyze_policy_headlines_with_deepseek(headlines, trade_date=effective_date)
    else:
        discovered = _sanitize_policy_payload(discovered_policy, trade_date=effective_date)
    merged = merge_policy_payloads(existing, discovered, trade_date=effective_date, cluster_watch=cluster_watch)
    save_dynamic_policy_themes(merged, path=path)
    return merged


def merge_policy_payloads(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    trade_date: str,
    cluster_watch: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    aliases = _merge_alias_dicts(existing.get("dynamic_aliases"), incoming.get("dynamic_aliases"))
    merged_aliases = merge_theme_aliases(aliases)
    in_boost = _normalize_theme_entries(incoming.get("boost_themes"), "weight", merged_aliases)
    in_squeeze = _normalize_theme_entries(incoming.get("squeeze_themes"), "penalty", merged_aliases)
    new_boost_names = {str(r["theme"]) for r in in_boost}
    new_squeeze_names = {str(r["theme"]) for r in in_squeeze}
    prev_boost = [r for r in (existing.get("boost_themes") or []) if str(r.get("theme")) not in new_squeeze_names]
    prev_squeeze = [r for r in (existing.get("squeeze_themes") or []) if str(r.get("theme")) not in new_boost_names]
    confirmed = {
        str(item.get("theme") or "").strip()
        for item in (cluster_watch or [])
        if float(item.get("cluster_score") or 0.0) >= 0.48
    }
    boost = _merge_theme_entries(prev_boost, in_boost, "weight", confirmed, max_val=0.85)
    squeeze = _merge_theme_entries(prev_squeeze, in_squeeze, "penalty", set(), max_val=0.75)
    return _sanitize_policy_payload(
        {
            "trade_date": trade_date,
            "updated_at": _cn_now_iso(),
            "ttl_days": int(incoming.get("ttl_days") or existing.get("ttl_days") or DEFAULT_POLICY_TTL_DAYS),
            "dynamic_aliases": aliases,
            "boost_themes": boost,
            "squeeze_themes": squeeze,
        },
        trade_date=trade_date,
    )


def build_funnel_policy_events(
    ths_hot_events: dict[str, Any] | None,
    dynamic_policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    aliases = merge_theme_aliases((dynamic_policy or {}).get("dynamic_aliases"))
    events: list[dict[str, Any]] = []
    for item in (ths_hot_events or {}).get("events") or []:
        title = _clean_text(item.get("title"), 80)
        if not title:
            continue
        raw_themes = [str(t).strip() for t in (item.get("themes") or []) if str(t).strip()]
        normalized = [normalize_theme_name(t, aliases) for t in raw_themes]
        events.append(
            {
                "title": title,
                "summary": _clean_text(item.get("investment_direction") or item.get("summary") or "", 120),
                "source": "ths_hot_event",
                "themes": [t for t in normalized if t and is_actionable_theme_name(t)],
                "weight": 0.55,
            }
        )
    return events


def _headlines_from_ths_events(ths_hot_events: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in (ths_hot_events or {}).get("events") or []:
        title = _clean_text(item.get("title"), 80)
        if title:
            rows.append(
                {
                    "title": title,
                    "summary": _clean_text(item.get("investment_direction") or item.get("summary") or "", 120),
                    "source": "同花顺热榜",
                }
            )
    return rows


def _fetch_eastmoney_telegraph(*, timeout: int) -> list[dict[str, Any]]:
    if os.getenv("POLICY_DISCOVERY_DISABLE_NETWORK", "").strip() == "1":
        return []
    params = {
        "client": "web",
        "biz": "web_724",
        "column": "350",
        "order": "1",
        "needInteractData": "0",
        "page_index": "1",
        "page_size": "25",
    }
    try:
        resp = requests.get(EASTMONEY_724_URL, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        items = ((resp.json() or {}).get("data") or {}).get("list") or []
        return [
            {
                "title": _clean_text(row.get("title"), 80),
                "summary": _clean_text(row.get("summary"), 120),
                "source": "东方财富7x24",
            }
            for row in items
            if _clean_text(row.get("title") or row.get("summary") or "", 120)
        ]
    except Exception as exc:
        logger.debug("eastmoney telegraph fetch skipped: %s", exc)
        return []


def _clean_text(value: Any, max_len: int = 120) -> str:
    raw = re.sub(r"[\r\n\t`{}]+", " ", str(value or "")).strip()
    return raw[:max_len]


def _dedupe_headlines(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("title") or item.get("summary") or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def _is_expired(payload: dict[str, Any], trade_date: str | None) -> bool:
    if not trade_date:
        return False
    saved_date = str(payload.get("trade_date") or "").strip()
    if not saved_date:
        return False
    try:
        delta = (date.fromisoformat(trade_date[:10]) - date.fromisoformat(saved_date[:10])).days
        ttl = int(payload.get("ttl_days") or DEFAULT_POLICY_TTL_DAYS)
        return delta > max(ttl, 1)
    except ValueError:
        return False


def _is_valid_dynamic_alias(theme: str, alias: str) -> bool:
    word = _clean_text(alias, 24)
    min_len = 4 if word.isascii() else 3
    if len(word) < min_len or not is_actionable_theme_name(word):
        return False
    owner = _STATIC_ALIAS_OWNERS.get(word.casefold())
    return owner is None or owner == theme


def _merge_alias_dicts(base: Any, extra: Any) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for source in (base or {}, extra or {}):
        if not isinstance(source, dict):
            continue
        for raw_theme, words in source.items():
            theme = str(raw_theme or "").strip()
            if theme not in THEME_ALIASES or not isinstance(words, list | tuple):
                continue
            cleaned = [_clean_text(w, 24) for w in words if _is_valid_dynamic_alias(theme, str(w or ""))]
            if cleaned:
                merged[theme] = list(dict.fromkeys([*merged.get(theme, []), *cleaned]))[:12]
    return merged


def _merge_theme_entries(
    existing: Any,
    incoming: Any,
    score_key: str,
    confirmed_themes: set[str],
    *,
    max_val: float = 0.85,
) -> list[dict[str, Any]]:
    by_theme: dict[str, dict[str, Any]] = {}
    for item in existing or []:
        if not isinstance(item, dict):
            continue
        theme = str(item.get("theme") or "").strip()
        if not theme:
            continue
        val = float(item.get(score_key) or 0.0)
        decayed = val if theme in confirmed_themes else round(val * 0.92, 4)
        if decayed >= 0.35:
            by_theme[theme] = {**item, "theme": theme, score_key: min(decayed, max_val)}
    for item in incoming or []:
        if not isinstance(item, dict):
            continue
        theme = str(item.get("theme") or "").strip()
        val = float(item.get(score_key) or 0.0)
        if theme and val >= 0.35:
            by_theme[theme] = {
                "theme": theme,
                score_key: round(min(max(val, 0.35), max_val), 4),
                "title": _clean_text(item.get("title"), 80),
                "source": _clean_text(item.get("source") or "policy", 24),
            }
    return sorted(by_theme.values(), key=lambda r: -float(r.get(score_key) or 0.0))[:10]


def _sanitize_policy_payload(payload: dict[str, Any], *, trade_date: str | None) -> dict[str, Any]:
    aliases = _merge_alias_dicts({}, payload.get("dynamic_aliases"))
    merged_aliases = merge_theme_aliases(aliases)
    boost = _normalize_theme_entries(payload.get("boost_themes"), "weight", merged_aliases, max_val=0.85)
    squeeze = _normalize_theme_entries(payload.get("squeeze_themes"), "penalty", merged_aliases, max_val=0.75)
    return {
        "trade_date": str(trade_date or payload.get("trade_date") or cn_today().isoformat()).strip(),
        "updated_at": str(payload.get("updated_at") or _cn_now_iso()),
        "ttl_days": int(payload.get("ttl_days") or DEFAULT_POLICY_TTL_DAYS),
        "dynamic_aliases": aliases,
        "boost_themes": boost,
        "squeeze_themes": squeeze,
    }


def _normalize_theme_entries(
    rows: Any,
    score_key: str,
    aliases: dict[str, tuple[str, ...]],
    *,
    max_val: float = 0.85,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        theme = normalize_theme_name(str(item.get("theme") or ""), aliases)
        if theme not in THEME_ALIASES:
            continue
        normalized.append({**item, "theme": theme})
    return _merge_theme_entries([], normalized, score_key, set(), max_val=max_val)


def _cn_now_iso() -> str:
    return datetime.now(_CN_TZ).isoformat(timespec="seconds")


def _empty_policy(trade_date: str | None = None) -> dict[str, Any]:
    return {
        "trade_date": str(trade_date or cn_today().isoformat()).strip(),
        "updated_at": "",
        "ttl_days": DEFAULT_POLICY_TTL_DAYS,
        "dynamic_aliases": {},
        "boost_themes": [],
        "squeeze_themes": [],
    }
