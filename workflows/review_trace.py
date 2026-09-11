"""Compact as-run decision trace for strong-move review."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from core.candidate_ranker import TRIGGER_LABELS
from core.candidate_tracks import best_candidate_entry_map
from core.cn_boards import is_supported_cn_board
from core.funnel_taxonomy import (
    REVIEW_STAGE_BASE_REJECT,
    REVIEW_STAGE_CANDIDATE_HIT,
    REVIEW_STAGE_RISK_BLOCK,
    REVIEW_STAGE_STRENGTH_MISS,
    REVIEW_STAGE_THEME_MISS,
    REVIEW_STAGE_TRIGGER_HIT,
    REVIEW_STAGE_TRIGGER_MISS,
    lane_label,
)
from core.review_shadow_lanes import attach_shadow_signal
from core.wyckoff_engine import sort_by_date_if_needed

REVIEW_TRACE_SCHEMA = "review_trace_v2"
#: 还能被 :func:`load_review_trace_artifact` 读进来的版本号。老号必须留着:v1 与 v2
#: 的行结构完全一致,v2 只是在头里多声明了一份 ``row_fields``。只把常量一升了事,
#: 22 份历史 trace 会全部变成「schema mismatch」——而效果检验的样本正是靠它们攒的。
REVIEW_TRACE_COMPATIBLE_SCHEMAS = frozenset({"review_trace_v1", REVIEW_TRACE_SCHEMA})

#: 每一行都会写的字段。逐行字段是分四批上线的(按 26 份历史产物实测):
#: ``trigger_labels``/``risk_signal``(以及逐行可选的 ``shadow_*``)从 2026-08-13 起、
#: ``close``/``layer3_quality_score``/``rps_fast``/``rps_slow`` 从 09-02 起、
#: ``risk_evaluated``/``l3_eligible_strict`` 从 09-08 起,而 ``schema_version``
#: 四批都写 v1。于是读侧分不清两件事:「这天的 writer 还没有这个字段」和「这只票
#: 取不到值」。一个照着 ``if row.get("rps_fast") is None: continue`` 写的筛子会
#: 静默丢掉 22 个交易日里的 15 个,再把剩下 7 个当成全样本报出来——把「尚未可知」
#: 读成「事实为空」,和 nan-passes-every-truthy-guard 是同一类病。把 writer 实际
#: 写了什么声明在 payload 头里,缺失就有据可查。
REVIEW_TRACE_ROW_FIELDS: tuple[str, ...] = (
    "name",
    "sector",
    "stage",
    "reason",
    "l1_eligible",
    "l2_eligible",
    "l3_eligible",
    "l3_eligible_strict",
    "l2_channel",
    "layer3_quality_score",
    "trigger_labels",
    "risk_signal",
    "risk_evaluated",
    "rps_fast",
    "rps_slow",
    "close",
)

#: 只出现在部分行上的字段:``entry`` 只有候选命中行有,``shadow_*`` 只有影子车道
#: 命中的行有。它们缺席是正常的,不能当成「writer 没有这个字段」,所以从行里反推
#: 字段集时要先把它们排除掉。
REVIEW_TRACE_PER_ROW_FIELDS = frozenset(
    {
        "entry",
        "shadow_lane",
        "shadow_score",
        "shadow_ranked",
        "shadow_reason",
        "shadow_policy_version",
    }
)

_BLOCKING_EXIT_SIGNALS = {"stop_loss", "distribution_warning", "upthrust_warning"}
_SHADOW_NEAR_L2_MAX_GAP_PCT = 10.0


def write_review_trace_artifact(inputs: Any, triggers: dict, metrics: dict, output_dir: str) -> Path | None:
    if not str(output_dir or "").strip():
        return None
    return dump_review_trace_artifact(build_review_trace(inputs, triggers, metrics), output_dir)


def dump_review_trace_artifact(payload: dict[str, Any], output_dir: str) -> Path | None:
    """落盘一份已构建好的 trace。

    与 build 分开:落库和落盘用同一份 payload,不重复走一遍全量决策行构建。
    """
    if not str(output_dir or "").strip():
        return None
    path = Path(output_dir) / f"review_trace_{payload['trade_date'].replace('-', '')}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temp_path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    temp_path.replace(path)
    return path


def build_review_trace(inputs: Any, triggers: dict, metrics: dict) -> dict[str, Any]:
    layers = inputs.layers
    candidates = inputs.candidates
    symbols = _decision_rows(inputs, triggers, metrics)
    config_payload = asdict(inputs.cfg)
    return {
        "schema_version": REVIEW_TRACE_SCHEMA,
        "market": "cn",
        "trade_date": inputs.window.end_trade_date.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "run": {
            "github_run_id": os.getenv("GITHUB_RUN_ID", ""),
            "github_run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", ""),
            "git_sha": os.getenv("GITHUB_SHA", ""),
        },
        "config_digest": _digest(config_payload),
        # 这一版 writer 逐行写了哪些字段。读侧靠它把「字段没上线」和「这只票没值」
        # 分开;新增逐行字段必须同时进 REVIEW_TRACE_ROW_FIELDS,否则声明就在骗人。
        "row_fields": list(REVIEW_TRACE_ROW_FIELDS),
        "policy": _review_policy(inputs.cfg),
        "data_quality": metrics.get("data_quality") or {},
        "market_context": _market_context(metrics),
        "counts": {
            "universe": len(inputs.pool.symbols),
            "trace_rows": len(symbols),
            "l1": len(layers.l1_passed),
            "l2": len(layers.l2_passed),
            "l3": len(layers.l3_passed),
            "candidates": len(candidates.candidate_entries),
        },
        "symbols": symbols,
    }


def load_review_trace_artifact(path: str | Path, expected_trade_date: date | str) -> dict[str, Any]:
    with gzip.open(Path(path), "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("schema_version") not in REVIEW_TRACE_COMPATIBLE_SCHEMAS:
        raise ValueError("review trace schema mismatch")
    if payload.get("market") != "cn":
        raise ValueError("review trace market mismatch")
    expected = expected_trade_date.isoformat() if isinstance(expected_trade_date, date) else str(expected_trade_date)
    if str(payload.get("trade_date") or "") != expected:
        raise ValueError(f"review trace date mismatch: expected={expected}, got={payload.get('trade_date')}")
    if not isinstance(payload.get("symbols"), dict):
        raise ValueError("review trace symbols missing")
    return payload


def trace_row_fields(payload: dict[str, Any]) -> frozenset[str]:
    """这份 trace 的 writer 实际写了哪些逐行字段。

    v2 起直接读头里的 ``row_fields``。v1 没有这份声明,退回从行里反推:同一个交易日
    内,非可选字段要么全行都有、要么全行都没有——26 份历史产物逐键数过,没有一个
    非可选键是部分行才有的,真正逐行可选的只有 ``entry`` 与 ``shadow_*``。所以取
    所有行键的交集、再去掉那几个逐行可选键,就是当天 writer 的字段集。

    交集而不是并集:并集会把「只有候选命中行才有的 entry」也算进 writer 字段,于是
    别的行缺 entry 又被读成「值缺失」,正是这个函数要消掉的那种混淆。
    """
    declared = payload.get("row_fields")
    if isinstance(declared, list | tuple | set | frozenset):
        names = frozenset(str(name) for name in declared if str(name or "").strip())
        # 空声明退回反推:行本身才是事实,声明为空只可能是 writer 出错,不该让读侧
        # 把每个字段都读成「没上线」。
        if names:
            return names
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict) or not symbols:
        return frozenset()
    shared: set[str] | None = None
    for row in symbols.values():
        keys = set(row.keys()) if isinstance(row, dict) else set()
        shared = keys if shared is None else (shared & keys)
        if not shared:
            return frozenset()
    return frozenset(shared or set()) - REVIEW_TRACE_PER_ROW_FIELDS


def trace_has_row_field(payload: dict[str, Any], field: str) -> bool:
    """``field`` 在这份 trace 里是「写过」还是「那天还没有这个字段」。

    历史 trace 里 ``rps_fast``/``risk_evaluated``/``l3_eligible_strict`` 全是 ``None``,
    与「查过但这只票没值」同形。要按字段拆样本、或要断言某个口径可评估,先问这里,
    别拿 ``row.get(field) is None`` 当判据——那会把「尚未可知」读成「事实为空」
    (memory insufficient-sample-is-not-failed-control)。
    """
    return str(field or "") in trace_row_fields(payload)


def _decision_rows(inputs: Any, triggers: dict, metrics: dict | None = None) -> dict[str, dict[str, Any]]:
    layers = inputs.layers
    candidates = inputs.candidates
    l1_set = set(layers.l1_passed)
    l2_set = set(layers.l2_passed)
    l3_set = set(layers.l3_passed)
    # 修复期(BEAR_REBOUND/PANIC_REPAIR*)Layer 3 从硬过滤降级为 +8 加分项,
    # l3_passed 会等于 l2_passed——那 6 天 L3/L2 恰好 100.0%,不是 bug 是设计。
    # 降级时 funnel_layers 把「照常过滤会留下谁」存进 benchmark_context,
    # 但此前没人落盘:每个降级日都白算了一次同日同水温的「L3 开 vs 关」对照。
    # 落成逐行三态,原地就能分组比前向收益,不必再去反推分数断层。
    l3_normal_set = _l3_normal_set(metrics)
    entry_map = best_candidate_entry_map(candidates.candidate_entries)
    hit_map = _trigger_labels(triggers)
    blocked = _blocked_exit_map(candidates.exit_signals)
    # 离场信号只对 L2 通过池 + Markup + 战略旁路算过,别的票 risk_signal 恒为空。
    # 不把「查过谁」记下来,复盘就会把「没查」读成「干净」:2026-09-04 全部 1246
    # 只结构强度不足的票 stop_loss 都是 0,而隔壁查过的池子 68% 带 stop_loss——
    # 这个反差是取值范围造成的,不是市场造成的。
    # 用 None 区分「字段缺失」和「查过 0 只」:`or []` 会把缺失也变成空集合,
    # 于是每一行都被断言成「没查过」——把缺失读成事实,正是这个字段要治的病。
    raw_evaluated = getattr(candidates, "exit_evaluated", None)
    evaluated = None if raw_evaluated is None else {str(code) for code in raw_evaluated}
    # watch_score 落进 trace:pre_breakout 车道的排序键。缺了它影子车道只能给
    # 常数分,31 只同分就无法做「取前 N 只看超额」的效果检验。
    # 用 getattr:回放路径自己拼候选对象,不一定带这个字段,缺了就退回不可排序标签。
    score_map = {str(k): v for k, v in (getattr(candidates, "l3_score_map", None) or {}).items()}
    return {
        code: attach_shadow_signal(
            _decision_row(
                code,
                inputs,
                l1_set,
                l2_set,
                l3_set,
                entry_map,
                hit_map,
                blocked,
                score_map,
                evaluated,
                l3_normal_set,
            ),
            near_l2_max_gap_pct=_SHADOW_NEAR_L2_MAX_GAP_PCT,
        )
        for code in inputs.pool.symbols
    }


def _decision_row(
    code: str,
    inputs: Any,
    l1_set: set[str],
    l2_set: set[str],
    l3_set: set[str],
    entry_map: dict[str, dict],
    hit_map: dict[str, list[str]],
    blocked: dict[str, dict],
    score_map: dict[str, Any] | None = None,
    evaluated: set[str] | None = None,
    l3_normal_set: set[str] | None = None,
) -> dict[str, Any]:
    name = str(inputs.ref_data.name_map.get(code, code)).strip() or code
    sector = str(inputs.ref_data.sector_map.get(code, "")).strip()
    base = {
        "name": name,
        "sector": sector,
        "l1_eligible": code in l1_set,
        "l2_eligible": code in l2_set,
        "l3_eligible": code in l3_set,
        # None=当天 L3 按硬过滤跑,l3_eligible 本身就是严格口径;
        # True/False=当天 L3 被降级,这一位是「照常过滤会不会留下它」的反事实。
        "l3_eligible_strict": code in l3_normal_set if l3_normal_set is not None else None,
        "l2_channel": str(inputs.layers.l2_channel_map.get(code, "")),
        "layer3_quality_score": _score_or_none((score_map or {}).get(code)),
        "trigger_labels": list(hit_map.get(code, [])),
        "risk_signal": str((blocked.get(code) or {}).get("signal") or ""),
        "risk_evaluated": code in evaluated if evaluated is not None else None,
        **_market_state(code, inputs),
    }
    if code not in inputs.all_df_map:
        return {**base, "stage": "数据失败", "reason": "日线拉取失败/超时"}
    if code not in l1_set:
        reason = _l1_rejection_reason(code, inputs)
        return {**base, "stage": REVIEW_STAGE_BASE_REJECT, "reason": reason}
    if code in entry_map:
        entry = _entry_payload(entry_map[code])
        return {**base, "stage": REVIEW_STAGE_CANDIDATE_HIT, "reason": _candidate_reason(entry), "entry": entry}
    if code not in l2_set:
        detail = str(inputs.layers.l2_rejections.get(code) or "八通道均未通过")
        return {**base, "stage": REVIEW_STAGE_STRENGTH_MISS, "reason": f"结构强度不足：{detail}"}
    if code not in l3_set:
        return {**base, "stage": REVIEW_STAGE_THEME_MISS, "reason": f"题材/行业共振不足（{sector or '未知行业'}）"}
    if code in blocked:
        return {**base, "stage": REVIEW_STAGE_RISK_BLOCK, "reason": _risk_reason(blocked[code], hit_map.get(code, []))}
    if code in hit_map:
        return {**base, "stage": REVIEW_STAGE_TRIGGER_HIT, "reason": "、".join(hit_map[code])}
    return {**base, "stage": REVIEW_STAGE_TRIGGER_MISS, "reason": "未触发 Spring/LPS/EVR/SOS 等买点确认"}


def _score_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _market_state(code: str, inputs: Any) -> dict[str, float | None]:
    """记录信号当日的动量与收盘价。

    效果检验要的是「同动量随机对照」:不留下当日 RPS,事后只能拿全市场当对照,
    会把择时读成选股(memory full-market-control-confounds-momentum)。RPS 在 L2
    已经算过,这里只是把它接出来,不重算。用 getattr:回放路径自己拼 layers 对象。
    """
    layers = inputs.layers
    return {
        "rps_fast": _score_or_none((getattr(layers, "rps_fast_map", None) or {}).get(code)),
        "rps_slow": _score_or_none((getattr(layers, "rps_slow_map", None) or {}).get(code)),
        "close": _last_close(inputs.all_df_map.get(code)),
    }


def _last_close(frame: pd.DataFrame | None) -> float | None:
    if frame is None or getattr(frame, "empty", True):
        return None
    close = pd.to_numeric(sort_by_date_if_needed(frame).get("close"), errors="coerce").dropna()
    return None if close.empty else round(float(close.iloc[-1]), 4)


def _l1_rejection_reason(code: str, inputs: Any) -> str:
    cfg = inputs.cfg
    name = str(inputs.ref_data.name_map.get(code, ""))
    if cfg.require_cn_main_or_chinext and not is_supported_cn_board(code, include_bse=cfg.include_bse_board):
        return "非A股目标板块代码"
    if "ST" in name.upper():
        return "ST股票"
    frame = sort_by_date_if_needed(inputs.all_df_map[code])
    cap_reason = _market_cap_reason(code, frame, inputs)
    if cap_reason:
        return cap_reason
    close = pd.to_numeric(frame.get("close"), errors="coerce").dropna()
    if not close.empty and float(close.iloc[-1]) < float(cfg.l1_min_close_price):
        return f"股价不足: {float(close.iloc[-1]):.2f} < {cfg.l1_min_close_price:.2f}"
    amount_reason = _amount_reason(frame, cfg)
    if amount_reason:
        return amount_reason
    financial = inputs.ref_data.financial_map.get(code) or {}
    if financial.get("roe") is not None and float(financial["roe"]) < -10:
        return f"财务准入失败: ROE={float(financial['roe']):.2f}% < -10%"
    if financial.get("debt_to_asset_ratio") is not None and float(financial["debt_to_asset_ratio"]) > 85:
        return f"财务准入失败: 资产负债率={float(financial['debt_to_asset_ratio']):.2f}% > 85%"
    return "未通过基础准入（综合条件不满足）"


def _market_cap_reason(code: str, frame: pd.DataFrame, inputs: Any) -> str:
    cap_map = inputs.ref_data.market_cap_map
    if not cap_map:
        return ""
    cfg = inputs.cfg
    cap = float(cap_map.get(code, 0.0) or 0.0)
    if cap < cfg.l1_delist_risk_cap_floor_yi:
        return f"市值不足: {cap:.2f}亿 < 退市风险地板{cfg.l1_delist_risk_cap_floor_yi:.2f}亿"
    if cap >= cfg.min_market_cap_yi:
        return ""
    avg_amount = _average_amount_wan(frame, cfg.amount_avg_window)
    if avg_amount < cfg.l1_cap_bypass_amount_wan:
        return f"市值{cap:.2f}亿且成交额不足小市值旁路{cfg.l1_cap_bypass_amount_wan:.1f}万"
    return ""


def _amount_reason(frame: pd.DataFrame, cfg: Any) -> str:
    amount = pd.to_numeric(frame.get("amount"), errors="coerce").dropna().tail(cfg.amount_avg_window)
    threshold = float(cfg.min_avg_amount_wan) * 10000.0
    if amount.empty or float(amount.mean()) < threshold:
        return f"成交额不足: {_average_amount_wan(frame, cfg.amount_avg_window):.1f}万 < {cfg.min_avg_amount_wan:.1f}万"
    positive = amount[amount > 0]
    if not cfg.amount_skew_check_enabled or len(positive) < 5:
        return ""
    skew = positive.skew()
    median_weak = float(positive.median()) < threshold * cfg.amount_median_min_ratio
    pass_days_weak = float((positive >= threshold).mean()) < cfg.amount_pass_days_min_ratio
    if pd.notna(skew) and float(skew) >= cfg.amount_skew_max and (median_weak or pass_days_weak):
        return "成交额由少数尖峰日扭曲，持续流动性不足"
    return ""


def _average_amount_wan(frame: pd.DataFrame, window: int) -> float:
    amount = pd.to_numeric(frame.get("amount"), errors="coerce").dropna().tail(window)
    return float(amount.mean()) / 10000.0 if not amount.empty else 0.0


def _trigger_labels(triggers: dict) -> dict[str, list[str]]:
    hit_map: dict[str, list[str]] = {}
    for trigger, label in TRIGGER_LABELS.items():
        for code, _score in triggers.get(trigger, []):
            labels = hit_map.setdefault(str(code), [])
            if label not in labels:
                labels.append(label)
    return hit_map


def _blocked_exit_map(exit_signals: dict) -> dict[str, dict]:
    return {
        str(code): dict(raw or {})
        for code, raw in (exit_signals or {}).items()
        if str((raw or {}).get("signal") or "").strip() in _BLOCKING_EXIT_SIGNALS
    }


def _entry_payload(entry: dict) -> dict[str, Any]:
    return {
        "code": str(entry.get("code") or ""),
        "entry_type": str(entry.get("entry_type") or entry.get("signal_key") or "candidate"),
        "signal_key": str(entry.get("signal_key") or ""),
        "track": str(entry.get("track") or ""),
        "score": _float(entry.get("score")),
        "opportunity": str(entry.get("opportunity") or ""),
        "timing": str(entry.get("timing") or ""),
        "risk": str(entry.get("risk") or ""),
    }


def _candidate_reason(entry: dict) -> str:
    entry_type = str(entry.get("entry_type") or "candidate")
    parts = [f"候选车道: {lane_label(entry_type) or entry_type}", f"score={entry.get('score', 0.0):.2f}"]
    parts.extend(str(entry[key]) for key in ("opportunity", "timing", "risk") if str(entry.get(key) or "").strip())
    return " | ".join(parts)


def _risk_reason(signal: dict, trigger_labels: list[str]) -> str:
    label = {
        "stop_loss": "触发结构止损",
        "distribution_warning": "触发Distribution派发警告",
        "upthrust_warning": "触发Upthrust/UTAD假突破派发警告",
    }.get(str(signal.get("signal") or ""), "触发风控硬剔除")
    parts = [label]
    if signal.get("price") is not None:
        parts.append(f"参考价={_float(signal.get('price')):.2f}")
    if trigger_labels:
        parts.append(f"买点确认={'、'.join(trigger_labels)}")
    if str(signal.get("reason") or "").strip():
        parts.append(str(signal["reason"]).strip())
    return " | ".join(parts)


def _review_policy(cfg: Any) -> dict[str, Any]:
    return {
        "min_market_cap_yi": cfg.min_market_cap_yi,
        "min_avg_amount_wan": cfg.min_avg_amount_wan,
        "l1_min_close_price": cfg.l1_min_close_price,
        "ma_long": cfg.ma_long,
        "rps_fast_min": cfg.rps_fast_min,
        "rps_slow_min": cfg.rps_slow_min,
        "shadow_near_l2_max_gap_pct": _SHADOW_NEAR_L2_MAX_GAP_PCT,
    }


def _market_context(metrics: dict[str, Any]) -> dict[str, Any]:
    context = metrics.get("benchmark_context") or {}
    tuned = context.get("tuned") or {}
    return {
        "regime": str(context.get("regime") or ""),
        "trade_mode": str(metrics.get("trade_mode") or ""),
        "min_avg_amount_wan": _float(tuned.get("min_avg_amount_wan")),
        "rps_fast_min": _float(tuned.get("rps_fast_min")),
        "rps_slow_min": _float(tuned.get("rps_slow_min")),
    }


def _l3_normal_set(metrics: dict | None) -> set[str] | None:
    """降级日 Layer 3 的严格口径通过集;非降级日返回 None。

    `None` 与 `set()` 必须区分:前者是「今天 L3 就是硬过滤,别去比」,后者是
    「今天降级了,而且严格口径一只都不留」。用 `or set()` 兜底会把前者也变成
    空集合,于是 20 个正常日全被读成「L3 本来会全灭」——把缺失读成事实。
    """
    context = (metrics or {}).get("benchmark_context") or {}
    raw = context.get("l3_passed_normal")
    if raw is None:
        return None
    return {str(code) for code in raw}


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
