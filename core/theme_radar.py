"""Long-horizon theme radar for strategic watchlists."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from math import log1p
from statistics import median
from typing import Any

import pandas as pd

from core._price_math import clamp as _clamp
from core._price_math import drawdown_pct as _drawdown_pct
from core._price_math import ret_pct as _ret_pct
from core.concept_filters import is_actionable_theme_name, is_user_facing_etf
from utils.safe import finite_float as _as_float

THEME_ALIASES: dict[str, tuple[str, ...]] = {
    "国产CPU": ("国产CPU", "CPU", "处理器", "龙芯", "海光", "飞腾", "鲲鹏", "x86", "risc-v", "riscv"),
    "MLCC被动元件": ("MLCC", "被动元件", "陶瓷电容", "片式电容", "电容电阻"),
    "AI PCB": ("AI PCB", "PCB", "印制电路板", "高多层板", "HDI", "载板", "覆铜板"),
    "先进封装": ("先进封装", "Chiplet", "CoWoS", "封测", "封装测试", "HBM封装"),
    "HBM存储": ("HBM", "存储芯片", "DRAM", "NAND", "存储器", "存储"),
    "液冷": ("液冷", "服务器液冷", "数据中心液冷", "冷板", "温控"),
    "服务器算力": ("AI服务器", "服务器", "算力租赁", "智算中心", "数据中心", "GPU服务器"),
    "芯片半导体": ("芯片", "半导体", "集成电路", "先进封装", "存储", "光刻胶", "第三代半导体", "semiconductor", "chip"),
    "AI算力": ("人工智能", "AI应用", "大模型", "算力", "数据中心", "服务器", "液冷", "PCB", "data center", "server"),
    "光模块": (
        "光模块",
        "光通信",
        "800G",
        "1.6T",
        "硅光",
        "CPO",
        "铜缆高速连接",
        "高速铜缆",
        "optical module",
        "silicon photonics",
    ),
    "机器人": ("机器人", "人形机器人", "减速器", "伺服", "机器视觉", "传感器", "robot", "robotics", "actuator"),
    "创新药医药": (
        "创新药",
        "生物医药",
        "医药",
        "CRO",
        "CXO",
        "ADC",
        "单抗",
        "重组蛋白",
        "化学制药",
        "生物制药",
        "中成药",
    ),
    "核聚变核电": ("可控核聚变", "核聚变", "核能核电", "核电", "人造太阳", "超导", "ITER"),
    "有色资源": (
        "稀土",
        "小金属",
        "铜",
        "铝",
        "钨",
        "锑",
        "钼",
        "黄金",
        "锂",
        "钴",
        "镍",
        "rare earth",
        "copper",
        "tungsten",
    ),
    "新能源": ("新能源", "储能", "光伏", "锂电", "固态电池", "风电", "电网设备"),
    "低空经济": ("低空经济", "无人机", "eVTOL", "飞行汽车", "通航"),
    "红利低波": ("红利", "高股息", "股息率", "低波", "低波红利", "央企红利", "dividend", "low volatility"),
    "价值蓝筹": ("价值", "蓝筹", "低估值", "破净", "中特估", "稳增长", "大盘价值", "value", "blue chip"),
    "大金融": ("银行", "保险", "券商", "证券", "多元金融", "金融科技", "brokerage", "insurance", "bank"),
    "公用事业": ("电力", "火电", "水电", "核电", "公用事业", "燃气", "供水", "水务", "utility", "power"),
    "煤炭能源": ("煤炭", "煤化工", "动力煤", "焦煤", "油气", "石油", "天然气", "coal", "oil", "gas"),
    "消费防御": ("食品饮料", "白酒", "乳业", "调味品", "中药", "医药商业", "consumer staples", "defensive"),
}


@dataclass(frozen=True)
class ThemeRadarConfig:
    top_themes: int = 12
    max_candidates_per_theme: int = 8
    min_theme_score: float = 0.45
    min_stock_score: float = 0.45
    max_rotation_themes: int = 5
    min_rotation_members: int = 3
    min_rotation_score: float = 0.65
    min_rotation_breadth: float = 0.55


@dataclass(frozen=True)
class ThemeScore:
    theme: str
    score: float
    state: str
    heat_score: float
    leader_score: float
    structure_score: float
    breadth_score: float
    persistence_score: float
    catalyst_score: float
    crowding_score: float
    member_count: int
    leader_count: int
    rotation_score: float
    rotation_state: str
    ret5: float
    ret20: float
    advancing_ratio_5d: float
    evidence: list[str]


@dataclass(frozen=True)
class StrategicCandidate:
    code: str
    name: str
    theme: str
    theme_score: float
    stock_score: float
    leader_score: float
    theme_rank: int
    ret60: float
    ret120: float
    ret250: float
    near_high_120d: bool
    breakout_age_days: int
    state: str
    reasons: list[str]


def normalize_theme_name(raw: str, aliases: dict[str, tuple[str, ...]] | None = None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if aliases is None:
        return _normalize_default_theme(text)
    return _normalize_theme(text, aliases)


@lru_cache(maxsize=8192)
def _normalize_default_theme(text: str) -> str:
    return _normalize_theme(text, THEME_ALIASES)


def _normalize_theme(text: str, aliases: dict[str, tuple[str, ...]]) -> str:
    matches: list[tuple[int, int, int, str]] = []
    for order, (theme, keys) in enumerate(aliases.items()):
        if _alias_in_text(text, theme):
            matches.append((1, len(theme), -order, theme))
        for key in keys:
            if key and _alias_in_text(text, key):
                matches.append((0, len(key), -order, theme))
    return max(matches)[3] if matches else text


def _alias_in_text(text: str, alias: str) -> bool:
    folded_text = text.casefold()
    folded_alias = alias.casefold()
    start = 0
    while (index := folded_text.find(folded_alias, start)) >= 0:
        end = index + len(folded_alias)
        left_ok = not _needs_ascii_boundary(folded_alias[:1]) or index == 0 or not _ascii_word(folded_text[index - 1])
        right_ok = (
            not _needs_ascii_boundary(folded_alias[-1:]) or end == len(folded_text) or not _ascii_word(folded_text[end])
        )
        if left_ok and right_ok:
            return True
        start = index + 1
    return False


def _needs_ascii_boundary(char: str) -> bool:
    return bool(char) and _ascii_word(char)


def _ascii_word(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def infer_event_themes(event: dict[str, Any], aliases: dict[str, tuple[str, ...]] | None = None) -> list[str]:
    text = " ".join(str(event.get(k, "") or "") for k in ("title", "summary", "content", "tags"))
    themes = [
        theme
        for theme, keys in (aliases or THEME_ALIASES).items()
        if _alias_in_text(text, theme) or any(key and _alias_in_text(text, key) for key in keys)
    ]
    return sorted(set(themes))


def build_theme_radar_snapshot(
    *,
    trade_date: str,
    concept_heat: list[dict[str, Any]],
    concept_history: dict[str, dict] | None,
    concept_map: dict[str, list[str]],
    sector_map: dict[str, str],
    df_map: dict[str, pd.DataFrame],
    events: list[dict[str, Any]] | None = None,
    name_map: dict[str, str] | None = None,
    config: ThemeRadarConfig | None = None,
) -> dict[str, Any]:
    cfg = config or ThemeRadarConfig()
    features = _stock_features(df_map)
    heat = _heat_by_theme(concept_heat)
    history = _history_by_theme(concept_history or {})
    event_map = _events_by_theme(events or [])
    member_index = _theme_member_index(concept_map, sector_map)
    themes = _theme_universe(heat, history, event_map, member_index)
    markets = {theme: _theme_market_metrics(member_index.get(theme, []), features) for theme in themes}
    rotation_scores = _rotation_scores(markets, heat)
    scores = [
        _score_theme(theme, heat, history, event_map, markets[theme], rotation_scores.get(theme, 0.0))
        for theme in themes
    ]
    ranked = sorted(scores, key=lambda item: item.score, reverse=True)[: cfg.top_themes]
    rotation_watch = _rotation_watch(scores, cfg)
    candidates = _build_candidates(ranked, member_index, features, name_map or {}, cfg)
    return {
        "trade_date": trade_date,
        "themes": [asdict(item) for item in ranked if item.score >= cfg.min_theme_score],
        "rotation_watch": [asdict(item) for item in rotation_watch],
        "strategic_candidates": [asdict(item) for item in candidates],
    }


def summarize_theme_radar(snapshot: dict[str, Any], limit: int = 5) -> str:
    themes = [x for x in (snapshot.get("themes") or []) if not is_user_facing_etf(name=str(x.get("theme") or ""))]
    themes = themes[:limit]
    if not themes:
        return "无明确中长线主线"
    return "；".join(f"{x['theme']} {x['score']:.2f}/{x['state']}" for x in themes)


def summarize_theme_rotation(snapshot: dict[str, Any], limit: int = 3) -> str:
    themes = [
        item
        for item in (snapshot.get("rotation_watch") or [])
        if not is_user_facing_etf(name=str(item.get("theme") or ""))
    ][:limit]
    if not themes:
        return "暂无显著短周期轮动"
    return "；".join(
        f"{item['theme']} {item['rotation_score']:.2f}/{item['rotation_state']}"
        f"（5日{item['ret5']:+.1f}%，上涨占比{item['advancing_ratio_5d']:.0%}）"
        for item in themes
    )


def _stock_features(df_map: dict[str, pd.DataFrame]) -> dict[str, dict[str, Any]]:
    rows = {code: _raw_stock_metrics(df) for code, df in df_map.items() if df is not None and not df.empty}
    rps60 = _rank_metric(rows, "ret60")
    rps120 = _rank_metric(rows, "ret120")
    rps250 = _rank_metric(rows, "ret250")
    for code, row in rows.items():
        row["rps60"] = rps60.get(code, 0.0)
        row["rps120"] = rps120.get(code, 0.0)
        row["rps250"] = rps250.get(code, 0.0)
        row["structure_score"] = _stock_structure_score(row)
        row["leader_score"] = _stock_leader_score(row)
    return rows


def _raw_stock_metrics(df: pd.DataFrame) -> dict[str, Any]:
    ordered = df.sort_values("date") if "date" in df.columns else df
    close_raw = ordered["close"] if "close" in ordered.columns else pd.Series(dtype=float)
    close = pd.to_numeric(close_raw, errors="coerce").dropna()
    return {
        "ret5": _ret_pct(close, 5),
        "ret10": _ret_pct(close, 10),
        "ret20": _ret_pct(close, 20),
        "ret60": _ret_pct(close, 60),
        "ret120": _ret_pct(close, 120),
        "ret250": _ret_pct(close, 250),
        "above_ma60": _above_ma(close, 60),
        "above_ma120": _above_ma(close, 120),
        "above_ma200": _above_ma(close, 200),
        "drawdown120": _drawdown_pct(close, 120),
        "near_high_120d": _near_high(close, 120),
        "breakout_age_days": _days_since_high(close, 120),
    }


def _rank_metric(rows: dict[str, dict[str, Any]], field: str) -> dict[str, float]:
    values = [(code, _as_float(row.get(field))) for code, row in rows.items()]
    values = [(code, value) for code, value in values if value is not None]
    if not values:
        return {}
    ordered = sorted(values, key=lambda item: item[1])
    denom = max(len(ordered) - 1, 1)
    return {code: idx / denom for idx, (code, _) in enumerate(ordered)}


def _stock_structure_score(row: dict[str, Any]) -> float:
    ma_score = sum(float(row.get(k, 0.0) or 0.0) for k in ("above_ma60", "above_ma120", "above_ma200")) / 3
    drawdown = _clamp(1.0 - max(float(row.get("drawdown120") or 0.0), 0.0) / 35.0)
    return _clamp(0.35 * row.get("rps120", 0.0) + 0.25 * row.get("rps250", 0.0) + 0.25 * ma_score + 0.15 * drawdown)


def _stock_leader_score(row: dict[str, Any]) -> float:
    rps_score = (
        0.20 * float(row.get("rps60", 0.0) or 0.0)
        + 0.35 * float(row.get("rps120", 0.0) or 0.0)
        + 0.45 * float(row.get("rps250", 0.0) or 0.0)
    )
    ret60 = _clamp((float(row.get("ret60") or 0.0) - 20.0) / 60.0)
    ret120 = _clamp((float(row.get("ret120") or 0.0) - 35.0) / 100.0)
    ret250 = _clamp((float(row.get("ret250") or 0.0) - 50.0) / 160.0)
    absolute_score = 0.25 * ret60 + 0.45 * ret120 + 0.30 * ret250
    near_high = float(row.get("near_high_120d", 0.0) or 0.0)
    structure = float(row.get("structure_score", 0.0) or 0.0)
    return _clamp(0.45 * rps_score + 0.25 * absolute_score + 0.15 * near_high + 0.15 * structure)


def _heat_by_theme(concept_heat: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    heat: dict[str, dict[str, Any]] = {}
    for rank, item in enumerate(concept_heat or [], start=1):
        theme = normalize_theme_name(str(item.get("name", "")))
        if not theme or not is_actionable_theme_name(theme):
            continue
        bucket = heat.setdefault(theme, {"score": 0.0, "concepts": [], "pct": 0.0, "inflow": 0.0})
        score = _heat_item_score(item, rank)
        bucket["score"] = max(float(bucket["score"]), score)
        bucket["pct"] = max(float(bucket["pct"]), _as_float(item.get("pct")) or 0.0)
        bucket["inflow"] += _as_float(item.get("net_inflow")) or 0.0
        bucket["concepts"].append(str(item.get("name", "")))
    return heat


def _heat_item_score(item: dict[str, Any], rank: int) -> float:
    pct_score = _clamp(((_as_float(item.get("pct")) or 0.0) + 2.0) / 9.0)
    inflow_yi = max((_as_float(item.get("net_inflow")) or 0.0) / 1e8, 0.0)
    flow_score = _clamp(log1p(inflow_yi) / 3.0)
    rank_score = _clamp(1.0 - (rank - 1) / 30.0)
    return 0.35 * pct_score + 0.40 * flow_score + 0.25 * rank_score


def _history_by_theme(history: dict[str, dict]) -> dict[str, dict[str, Any]]:
    dates = sorted(history.keys(), reverse=True)
    if not dates:
        return {}
    latest = _themes_for_day(history.get(dates[0], {}))
    result: dict[str, dict[str, Any]] = {}
    for theme in latest:
        streak = _theme_streak(theme, dates, history)
        appearances = sum(1 for d in dates[:10] if theme in _themes_for_day(history.get(d, {})))
        result[theme] = {"streak": streak, "score": _clamp(0.65 * streak / 5.0 + 0.35 * appearances / 10.0)}
    return result


def _themes_for_day(day: dict[str, Any]) -> set[str]:
    return {theme for name in day.keys() if (theme := normalize_theme_name(name)) and is_actionable_theme_name(theme)}


def _theme_streak(theme: str, dates: list[str], history: dict[str, dict]) -> int:
    streak = 0
    for trade_date in dates:
        if theme not in _themes_for_day(history.get(trade_date, {})):
            break
        streak += 1
    return streak


def _events_by_theme(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        themes = event.get("themes") or infer_event_themes(event)
        for theme in themes:
            normalized = normalize_theme_name(str(theme))
            if is_actionable_theme_name(normalized):
                grouped.setdefault(normalized, []).append(event)
    return grouped


def _theme_universe(*parts: Any) -> list[str]:
    themes: set[str] = set()
    for part in parts[:3]:
        themes.update(str(k) for k in part.keys() if str(k).strip())
    member_index = parts[3] if len(parts) > 3 else {}
    themes.update(str(k) for k in member_index.keys() if str(k).strip())
    return sorted(t for t in themes if t and is_actionable_theme_name(t))


def _score_theme(
    theme: str,
    heat: dict[str, dict[str, Any]],
    history: dict[str, dict[str, Any]],
    events: dict[str, list[dict[str, Any]]],
    market: dict[str, float],
    rotation_score: float,
) -> ThemeScore:
    heat_score = float((heat.get(theme) or {}).get("score") or 0.0)
    persistence = float((history.get(theme) or {}).get("score") or 0.0)
    catalyst = _catalyst_score(events.get(theme, []))
    crowding = _crowding_score(market, heat.get(theme) or {})
    score = _clamp(
        0.24 * market["structure_score"]
        + 0.22 * market["leader_score"]
        + 0.18 * market["breadth_score"]
        + 0.16 * persistence
        + 0.10 * catalyst
        + 0.10 * heat_score
        - 0.08 * crowding
    )
    return ThemeScore(
        theme=theme,
        score=round(score, 4),
        state=_theme_state(score, persistence, crowding, market["leader_score"]),
        heat_score=round(heat_score, 4),
        leader_score=round(market["leader_score"], 4),
        structure_score=round(market["structure_score"], 4),
        breadth_score=round(market["breadth_score"], 4),
        persistence_score=round(persistence, 4),
        catalyst_score=round(catalyst, 4),
        crowding_score=round(crowding, 4),
        member_count=int(market["member_count"]),
        leader_count=int(market["leader_count"]),
        rotation_score=round(rotation_score, 4),
        rotation_state=_rotation_state(rotation_score),
        ret5=round(float(market["ret5"]), 4),
        ret20=round(float(market["ret20"]), 4),
        advancing_ratio_5d=round(float(market["advancing_ratio_5d"]), 4),
        evidence=_theme_evidence(theme, heat, history, events),
    )


def _rotation_scores(markets: dict[str, dict[str, float]], heat: dict[str, dict[str, Any]]) -> dict[str, float]:
    eligible = {theme: row for theme, row in markets.items() if row.get("member_count", 0.0) > 0}
    ranks = {field: _rank_metric(eligible, field) for field in ("ret5", "ret10", "ret20", "advancing_ratio_5d")}
    heat_rows = {theme: {"score": float((heat.get(theme) or {}).get("score") or 0.0)} for theme in eligible}
    heat_rank = _rank_metric(heat_rows, "score")
    return {
        theme: _clamp(
            0.30 * ranks["ret5"].get(theme, 0.0)
            + 0.25 * ranks["ret10"].get(theme, 0.0)
            + 0.15 * ranks["ret20"].get(theme, 0.0)
            + 0.20 * ranks["advancing_ratio_5d"].get(theme, 0.0)
            + 0.10 * heat_rank.get(theme, 0.0)
        )
        for theme in eligible
    }


def _rotation_watch(scores: list[ThemeScore], cfg: ThemeRadarConfig) -> list[ThemeScore]:
    eligible = [
        item
        for item in scores
        if item.member_count >= cfg.min_rotation_members
        and item.rotation_score >= cfg.min_rotation_score
        and item.ret5 > 0
        and item.advancing_ratio_5d >= cfg.min_rotation_breadth
    ]
    return sorted(eligible, key=lambda item: (item.rotation_score, item.ret5), reverse=True)[: cfg.max_rotation_themes]


def _rotation_state(score: float) -> str:
    if score >= 0.80:
        return "surging"
    if score >= 0.65:
        return "rising"
    return "quiet"


def _theme_member_index(concept_map: dict[str, list[str]], sector_map: dict[str, str]) -> dict[str, list[str]]:
    index: dict[str, set[str]] = {}
    for code, concepts in concept_map.items():
        for concept in concepts or []:
            theme = normalize_theme_name(concept)
            if theme and is_actionable_theme_name(theme):
                index.setdefault(theme, set()).add(str(code))
    for code, sector in sector_map.items():
        theme = normalize_theme_name(str(sector))
        if theme and is_actionable_theme_name(theme):
            index.setdefault(theme, set()).add(str(code))
    return {theme: sorted(members) for theme, members in index.items()}


def _theme_market_metrics(members: list[str], features: dict[str, dict[str, Any]]) -> dict[str, float]:
    rows = [features[code] for code in members if code in features]
    if not rows:
        return {
            "member_count": 0,
            "leader_count": 0,
            "leader_score": 0.0,
            "structure_score": 0.0,
            "breadth_score": 0.0,
            "ret5": 0.0,
            "ret10": 0.0,
            "ret20": 0.0,
            "advancing_ratio_5d": 0.0,
        }
    structures = sorted((float(row["structure_score"]) for row in rows), reverse=True)[:20]
    leaders = sorted((float(row["leader_score"]) for row in rows), reverse=True)[:20]
    breadth = [_breadth_unit(row) for row in rows]
    ret5_values = [_as_float(row.get("ret5")) or 0.0 for row in rows]
    ret10_values = [_as_float(row.get("ret10")) or 0.0 for row in rows]
    ret20_values = [_as_float(row.get("ret20")) or 0.0 for row in rows]
    return {
        "member_count": float(len(rows)),
        "leader_count": float(sum(1 for row in rows if float(row.get("leader_score", 0.0) or 0.0) >= 0.70)),
        "leader_score": float(median(leaders)),
        "structure_score": float(median(structures)),
        "breadth_score": float(sum(breadth) / len(breadth)),
        "ret5": float(median(ret5_values)),
        "ret10": float(median(ret10_values)),
        "ret20": float(median(ret20_values)),
        "advancing_ratio_5d": float(sum(value > 0 for value in ret5_values) / len(ret5_values)),
    }


def _breadth_unit(row: dict[str, Any]) -> float:
    ma = sum(float(row.get(k, 0.0) or 0.0) for k in ("above_ma60", "above_ma120", "above_ma200")) / 3
    trend = 1.0 if (_as_float(row.get("ret60")) or 0.0) > 0 else 0.0
    return 0.70 * ma + 0.30 * trend


def _catalyst_score(events: list[dict[str, Any]]) -> float:
    if not events:
        return 0.0
    sources = {str(e.get("source", "") or e.get("domain", "")).strip() for e in events}
    return _clamp(0.55 * min(len(events), 6) / 6.0 + 0.45 * min(len(sources), 4) / 4.0)


def _crowding_score(market: dict[str, float], heat: dict[str, Any]) -> float:
    ret20_score = _clamp((float(market.get("ret20", 0.0)) - 35.0) / 35.0)
    pct_score = _clamp((float(heat.get("pct", 0.0) or 0.0) - 7.0) / 6.0)
    return max(ret20_score, pct_score)


def _theme_state(score: float, persistence: float, crowding: float, leader_score: float) -> str:
    if score >= 0.70 and crowding >= 0.65:
        return "overheated"
    if score >= 0.75 and leader_score >= 0.65:
        return "extension"
    if score >= 0.65 and (persistence >= 0.35 or leader_score >= 0.65):
        return "confirmed"
    if score >= 0.45:
        return "observe"
    return "decay"


def _theme_evidence(
    theme: str,
    heat: dict[str, dict[str, Any]],
    history: dict[str, dict[str, Any]],
    events: dict[str, list[dict[str, Any]]],
) -> list[str]:
    evidence: list[str] = []
    if theme in heat:
        evidence.append("heat:" + ",".join((heat[theme].get("concepts") or [])[:3]))
    if theme in history:
        evidence.append(f"streak:{int(history[theme].get('streak') or 0)}")
    evidence.extend("event:" + str(e.get("title", ""))[:60] for e in events.get(theme, [])[:2])
    return evidence[:5]


def _build_candidates(
    themes: list[ThemeScore],
    member_index: dict[str, list[str]],
    features: dict[str, dict[str, Any]],
    name_map: dict[str, str],
    cfg: ThemeRadarConfig,
) -> list[StrategicCandidate]:
    candidates: list[StrategicCandidate] = []
    for theme in themes:
        if theme.score < cfg.min_theme_score:
            continue
        rows = _candidate_rows(theme, member_index, features, name_map)
        for row in rows[: cfg.max_candidates_per_theme]:
            score = _clamp(0.50 * row["leader_score"] + 0.30 * row["structure_score"] + 0.20 * theme.score)
            if score >= cfg.min_stock_score:
                candidates.append(_candidate_from_row(row, theme, score))
    return _rank_unique_candidates(candidates)


def _rank_unique_candidates(candidates: list[StrategicCandidate]) -> list[StrategicCandidate]:
    best_by_code: dict[str, StrategicCandidate] = {}
    for candidate in candidates:
        current = best_by_code.get(candidate.code)
        if current is None or _candidate_quality_key(candidate) > _candidate_quality_key(current):
            best_by_code[candidate.code] = candidate
    return sorted(best_by_code.values(), key=_candidate_sort_key)


def _candidate_quality_key(candidate: StrategicCandidate) -> tuple[float, float, float, int]:
    return (
        float(candidate.stock_score),
        float(candidate.theme_score),
        float(candidate.leader_score),
        -int(candidate.theme_rank),
    )


def _candidate_sort_key(candidate: StrategicCandidate) -> tuple[float, float, float, int, str]:
    return (
        -float(candidate.stock_score),
        -float(candidate.theme_score),
        -float(candidate.leader_score),
        int(candidate.theme_rank),
        candidate.code,
    )


def _candidate_rows(
    theme: ThemeScore,
    member_index: dict[str, list[str]],
    features: dict[str, dict[str, Any]],
    name_map: dict[str, str],
) -> list[dict[str, Any]]:
    rows = []
    for code in member_index.get(theme.theme, []):
        if code in features:
            rows.append({"code": code, "name": name_map.get(code, code), **features[code]})
    rows = sorted(rows, key=lambda row: (row["leader_score"], row["structure_score"]), reverse=True)
    for idx, row in enumerate(rows, start=1):
        row["theme_rank"] = idx
    return rows


def _candidate_from_row(row: dict[str, Any], theme: ThemeScore, score: float) -> StrategicCandidate:
    reasons = [
        f"RPS60/120/250={row.get('rps60', 0.0):.2f}/{row.get('rps120', 0.0):.2f}/{row.get('rps250', 0.0):.2f}",
        f"Ret60/120/250={float(row.get('ret60') or 0.0):.0f}%/{float(row.get('ret120') or 0.0):.0f}%/{float(row.get('ret250') or 0.0):.0f}%",
        f"主题内排名={int(row.get('theme_rank') or 0)}",
        f"近120日新高={int(bool(row.get('near_high_120d')))} / 距新高{int(row.get('breakout_age_days') or 0)}日",
        f"DD120={float(row.get('drawdown120') or 0.0):.1f}%",
    ]
    return StrategicCandidate(
        code=str(row["code"]),
        name=str(row.get("name") or row["code"]),
        theme=theme.theme,
        theme_score=theme.score,
        stock_score=round(score, 4),
        leader_score=round(float(row.get("leader_score") or 0.0), 4),
        theme_rank=int(row.get("theme_rank") or 0),
        ret60=round(float(row.get("ret60") or 0.0), 4),
        ret120=round(float(row.get("ret120") or 0.0), 4),
        ret250=round(float(row.get("ret250") or 0.0), 4),
        near_high_120d=bool(row.get("near_high_120d")),
        breakout_age_days=int(row.get("breakout_age_days") or 0),
        state=theme.state,
        reasons=reasons,
    )


def _above_ma(close: pd.Series, window: int) -> float:
    if close.empty:
        return 0.0
    ma = close.rolling(window, min_periods=max(3, min(window, len(close)))).mean().iloc[-1]
    return 1.0 if pd.notna(ma) and float(close.iloc[-1]) >= float(ma) else 0.0


def _near_high(close: pd.Series, lookback: int, tolerance_pct: float = 12.0) -> float:
    if close.empty:
        return 0.0
    return 1.0 if _drawdown_pct(close, lookback) <= tolerance_pct else 0.0


def _days_since_high(close: pd.Series, lookback: int) -> int:
    recent = close.tail(max(lookback, 1)).reset_index(drop=True)
    if recent.empty:
        return 999
    return int(len(recent) - 1 - int(recent.idxmax()))
