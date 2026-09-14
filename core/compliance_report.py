"""Compliance-safe market brief generation for Step3.

The raw Wyckoff report remains the internal artifact.  The public/compliance
brief is generated from a deliberately de-identified payload: market regime,
style distribution, sector aggregates, and risk notes only.  The cheap model
is a wording assistant, not a decision maker; a deterministic validator and
template fallback own the safety boundary.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from utils.safe import finite_float

EFFICIENCY_PROVIDER = "efficiency"
DEFAULT_MAX_OUTPUT_TOKENS = 2048
logger = logging.getLogger(__name__)

_STOCK_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})(?:[-/.年])(\d{1,2})(?:[-/.月])(\d{1,2})日?(?!\d)")
_PROHIBITED_TERMS = (
    "模型",
    "候选池",
    "操作池",
    "RAG",
    "完整研报",
    "内部流程",
    "样本入库",
    "脱敏",
    "买入",
    "卖出",
    "建仓",
    "加仓",
    "清仓",
    "减仓",
    "止损",
    "目标价",
    "参考价",
    "强烈推荐",
    "重点推荐",
    "明日买",
    "可操作",
    "PROBE",
    "ATTACK",
    "EXIT",
    "TRIM",
)
# 以 ⚠️ 起头是有意的：utils.feishu_report_card._WARNING_PREFIXES 认这个前缀，
# 会把它渲染成灰色小字脚注而不是正文段落。改动前缀会让免责声明退回正文全权重。
_COMPLIANCE_DISCLAIMER = (
    "⚠️ 本简报仅用于市场研究与信息交流，不构成投资建议；"
    "内容可能存在遗漏或偏差，请结合公开信息独立判断。股市有风险，投资需谨慎。"
)
ComplianceLLMCaller = Callable[..., str]


@dataclass(frozen=True)
class ComplianceLLMConfig:
    provider: str
    api_key: str
    model: str
    base_url: str
    source: str
    retries: int = 1
    timeout_seconds: int = 90
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


@dataclass(frozen=True)
class ComplianceValidation:
    ok: bool
    reasons: tuple[str, ...] = ()


def fmt_pct(value: Any) -> str:
    num = finite_float(value)
    if num is None:
        return "待更新"
    sign = "+" if num >= 0 else ""
    return f"{sign}{num:.2f}%"


def _fmt_number(value: Any, digits: int = 2) -> str:
    num = finite_float(value)
    if num is None:
        return "待更新"
    return f"{num:.{digits}f}"


def _fmt_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    text = raw.replace("/", "-").replace(".", "-")
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    match = _DATE_RE.search(text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
        year, month, day = text.split("-")
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return raw


def _regime_label(regime: Any) -> str:
    mapping = {
        "RISK_ON": "短线过热禁追",
        "BEAR_REBOUND": "熊市反抽",
        "NEUTRAL": "中性震荡",
        "RISK_OFF": "防守降温",
        "CRASH": "极端风险",
        "BLACK_SWAN": "异常冲击",
    }
    key = str(regime or "NEUTRAL").strip().upper() or "NEUTRAL"
    return mapping.get(key, key)


def _safe_text(value: Any, fallback: str = "待更新") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _score_bucket(score: float) -> str:
    if score >= 0.75:
        return "高"
    if score >= 0.45:
        return "中"
    return "低"


def _bucket_phrase(bucket: Any) -> str:
    mapping = {"高": "偏高", "中": "中等", "低": "偏低"}
    return mapping.get(str(bucket or "").strip(), "待更新")


def _market_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    breadth = ctx.get("breadth", {}) or {}
    return {
        "regime": str(ctx.get("regime", "NEUTRAL") or "NEUTRAL").strip().upper(),
        "regime_label": _regime_label(ctx.get("regime", "NEUTRAL")),
        "close": _fmt_number(ctx.get("close")),
        "ma50": _fmt_number(ctx.get("ma50")),
        "ma200": _fmt_number(ctx.get("ma200")),
        "ma50_slope_5d": fmt_pct(ctx.get("ma50_slope_5d")),
        "main_today_pct": fmt_pct(ctx.get("main_today_pct")),
        "recent3_cum_pct": fmt_pct(ctx.get("recent3_cum_pct")),
        "breadth_ratio": fmt_pct(breadth.get("ratio_pct")),
        "breadth_delta": fmt_pct(breadth.get("delta_pct")),
        "volume_ratio": _fmt_number(ctx.get("main_vol_ratio_5_20")),
        "volume_state": str(ctx.get("main_volume_state", "") or "").strip() or "待更新",
        "smallcap_today_pct": fmt_pct(ctx.get("smallcap_today_pct")),
        "smallcap_recent3_cum_pct": fmt_pct(ctx.get("smallcap_recent3_cum_pct")),
        "pv_summary": _safe_text(ctx.get("market_pv_summary")),
        "pv_outlook": _safe_text(ctx.get("market_pv_outlook")),
    }


def build_public_payload(
    *,
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str] | None = None,
    rag_veto_count: int = 0,
) -> dict[str, Any]:
    """Build a de-identified payload for the compliance brief."""

    ctx = benchmark_context or {}
    df = selected_df.copy() if isinstance(selected_df, pd.DataFrame) else pd.DataFrame()
    ops_set = {str(code).strip() for code in (ops_codes or []) if str(code).strip()}
    trade_date = _fmt_trade_date(ctx.get("trade_date") or ctx.get("end_trade_date") or "")
    payload: dict[str, Any] = {
        "trade_date": trade_date,
        "market": _market_payload(ctx),
        "sample_stats": {
            "candidate_count": int(len(df)),
            "springboard_count": int(len(ops_set)),
            "rag_veto_count": int(max(rag_veto_count, 0)),
        },
        "style_stats": {},
        "sector_stats": [],
        "risk_flags": [],
    }

    if not df.empty:
        payload["style_stats"] = _style_stats(df)
        payload["trigger_stats"] = _trigger_stats(df)
        payload["sector_stats"] = _sector_stats(df)

    payload["risk_flags"] = _risk_flags(payload)
    return payload


def _style_stats(df: pd.DataFrame) -> dict[str, int]:
    track_series = df.get("track", pd.Series(dtype=str)).astype(str).str.strip()
    return {
        "trend_count": int((track_series == "Trend").sum()),
        "accum_count": int((track_series == "Accum").sum()),
        "unknown_count": int((~track_series.isin(["Trend", "Accum"])).sum()),
    }


def _trigger_stats(df: pd.DataFrame) -> dict[str, int]:
    tag_series = df.get("tag", pd.Series(dtype=str)).astype(str).str.lower()
    return {
        "sos_count": int(tag_series.str.contains("sos|点火|突破", regex=True).sum()),
        "spring_count": int(tag_series.str.contains("spring", regex=True).sum()),
        "lps_count": int(tag_series.str.contains("lps", regex=True).sum()),
        "evr_count": int(tag_series.str.contains("evr", regex=True).sum()),
    }


def _sector_stats(df: pd.DataFrame) -> list[dict[str, Any]]:
    industry = _text_series(df, "industry")
    industry = industry[industry != ""]
    if industry.empty:
        return []

    score = _candidate_score_series(df).reindex(industry.index).fillna(0.0)
    grouped = (
        pd.DataFrame({"industry": industry, "score": score})
        .groupby("industry", as_index=False)
        .agg(sample_count=("industry", "count"), avg_score=("score", "mean"))
        .sort_values(["sample_count", "avg_score"], ascending=[False, False])
        .head(5)
    )
    return [
        {
            "industry": str(row["industry"]),
            "sample_count": int(row["sample_count"]),
            "score_bucket": _score_bucket(float(row["avg_score"])),
        }
        for _, row in grouped.iterrows()
    ]


def _text_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series([""] * len(df), index=df.index, dtype=str)
    return df[column].astype(str).str.strip()


def _candidate_score_series(df: pd.DataFrame) -> pd.Series:
    priority_raw = df["priority_score"] if "priority_score" in df.columns else pd.Series(pd.NA, index=df.index)
    funnel_raw = df["funnel_score"] if "funnel_score" in df.columns else pd.Series(pd.NA, index=df.index)
    score = _finite_numeric_series(priority_raw, df.index)
    return score.where(score.notna(), _finite_numeric_series(funnel_raw, df.index))


def _finite_numeric_series(raw: Any, index: pd.Index) -> pd.Series:
    converted = pd.to_numeric(raw, errors="coerce")
    series = converted if isinstance(converted, pd.Series) else pd.Series(converted, index=index)
    series = series.reindex(index)
    finite_mask = series.map(lambda value: math.isfinite(float(value)) if pd.notna(value) else False)
    return series.where(finite_mask)


def _risk_flags(payload: dict[str, Any]) -> list[str]:
    flags = []
    regime = payload["market"]["regime"]
    if regime in {"RISK_OFF", "CRASH", "BLACK_SWAN"}:
        flags.append("市场风险偏高，弱势环境下假突破与流动性折价需要防范")
    if payload["sample_stats"]["rag_veto_count"] > 0:
        flags.append("题材持续性仍需观察，需防范潜在负面因素扰动")
    if payload["sample_stats"]["candidate_count"] <= 0:
        flags.append("结构信号不足，市场方向需要等待更多确认")
    return flags or ["避免根据单日量价变化过度外推，仍需观察需求延续性"]


def _render_payload_text(payload: dict[str, Any]) -> str:
    lines = [
        "请写成可直接转发给普通读者的市场观察，不要提模型、候选池、操作池、RAG或内部流程。",
        f"报告日期: {payload.get('trade_date') or '待更新'}",
        *_render_market_payload_lines(payload.get("market") or {}),
        *_render_wyckoff_payload_lines(payload),
        *_render_sector_payload_lines(payload.get("sector_stats") or []),
        *_render_risk_payload_lines(payload.get("risk_flags") or []),
    ]
    return "\n".join(lines)


def _render_market_payload_lines(market: dict[str, Any]) -> list[str]:
    return [
        "大盘指标:",
        f"- 市场状态={market.get('regime_label', market.get('regime', 'NEUTRAL'))}",
        f"- 收盘={market.get('close')} | MA50={market.get('ma50')} | MA200={market.get('ma200')}",
        f"- 当日涨跌={market.get('main_today_pct')} | 近3日={market.get('recent3_cum_pct')}",
        f"- 广度={market.get('breadth_ratio')} | 广度变化={market.get('breadth_delta')}",
        f"- 量能={market.get('volume_state')} | 5/20量比={market.get('volume_ratio')}",
        f"- 小盘当日={market.get('smallcap_today_pct')} | 小盘近3日={market.get('smallcap_recent3_cum_pct')}",
        f"- 量价摘要={market.get('pv_summary')}",
        f"- 后续观察={market.get('pv_outlook')}",
    ]


def _render_wyckoff_payload_lines(payload: dict[str, Any]) -> list[str]:
    style = payload.get("style_stats") or {}
    trigger = payload.get("trigger_stats") or {}
    sample = payload.get("sample_stats") or {}
    return [
        "威科夫结构温度:",
        (
            "- 风格="
            f"Trend:{style.get('trend_count', 0)}, "
            f"Accum:{style.get('accum_count', 0)}, "
            f"Unknown:{style.get('unknown_count', 0)}"
        ),
        (
            "- 结构触发="
            f"强势推进确认:{trigger.get('sos_count', 0)}, "
            f"下探回收测试:{trigger.get('spring_count', 0)}, "
            f"回踩不破测试:{trigger.get('lps_count', 0)}, "
            f"缩量回撤测试:{trigger.get('evr_count', 0)}"
        ),
        f"- 结构样本数={sample.get('candidate_count', 0)}",
    ]


def _render_sector_payload_lines(sector_lines: list[dict[str, Any]]) -> list[str]:
    lines = [
        "板块热度:",
    ]
    if sector_lines:
        for item in sector_lines:
            lines.append(
                f"- {item.get('industry')} | sample_count={item.get('sample_count')} | score_bucket={item.get('score_bucket')}"
            )
    else:
        lines.append("- 无明显行业聚集")
    return lines


def _render_risk_payload_lines(risk_flags: list[str]) -> list[str]:
    lines = []
    lines.append("风险提示:")
    for item in risk_flags:
        lines.append(f"- {item}")
    return lines


def _system_prompt() -> str:
    return """你是威科夫方法市场观察简报编辑，只能基于输入的脱敏市场指标写公开市场研究摘要。

硬规则：
- 不得输出任何股票代码、股票名称、个股名单或个股排序。
- 不得给出买入、卖出、建仓、加仓、清仓、减仓、止损、目标价、参考价等交易指令。
- 不得承诺收益，不得暗示确定性上涨。
- 不要提“模型、候选池、操作池、RAG、完整研报、内部流程、样本入库”等背景词。
- 允许使用威科夫语气分析指数结构：供应、需求、承接、测试、吸筹、推进、回撤、派发压力。
- 若引用结构触发，必须翻成普通读者能理解的中文含义，不要堆英文缩写。
- 若正文需要写日期，只能使用输入里的“报告日期”，不得编造、脱敏或改写日期。
- 输出应像一篇可直接转发的短评，普通读者无需知道系统背景也能读懂。
- 输出中文 Markdown，结构固定为四段：大盘结构、威科夫解读、观察要点、风险提示。

精简要求（推送渠道已单独显示标题和日期，正文重复就是噪音）：
- 不要写正文大标题，不要单独写一行日期，直接从第一个小节标题开始。
- 每小节最多 3 条，每条一句话；没有输入数据支撑的泛泛而谈一律不写。
- 每条都要落到输入里的具体数值或计数上，不要写“需要持续观察”“仍需时间验证”这类空话。
- 不要复述输入的字段名，也不要为没有数据的维度硬凑段落。
"""


def _dates_in_text(text: str) -> set[str]:
    dates = set()
    for match in _DATE_RE.finditer(text or ""):
        year, month, day = match.groups()
        dates.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
    return dates


def validate_compliance_report(
    text: str,
    *,
    forbidden_names: list[str] | None = None,
    expected_trade_date: str | None = None,
) -> ComplianceValidation:
    reasons: list[str] = []
    body = text or ""
    if _STOCK_CODE_RE.search(body):
        reasons.append("contains_stock_code")
    for term in _PROHIBITED_TERMS:
        if term in body:
            reasons.append(f"contains_term:{term}")
            break
    for name in forbidden_names or []:
        clean = str(name or "").strip()
        if len(clean) >= 2 and clean in body:
            reasons.append("contains_stock_name")
            break
    expected_date = _fmt_trade_date(expected_trade_date)
    if expected_date:
        bad_dates = _dates_in_text(body) - {expected_date}
        if bad_dates:
            reasons.append("contains_wrong_date")
    return ComplianceValidation(ok=not reasons, reasons=tuple(reasons))


def render_compliance_fallback(payload: dict[str, Any]) -> str:
    """
    确定性模板。

    正文不再写大标题和日期：飞书卡片头部、企微/钉钉的 markdown 首行都由
    `title` 参数单独渲染，正文重复一遍等于每天多两行噪音。而且
    `## 今日市场观察简报` 会被卡片的 `_section_icon` 判成 🟡（命中“观察”），
    和真正的待确认级别混在一起。
    """
    market = payload.get("market") or {}
    lines = [
        *_fallback_market_lines(market),
        *_fallback_wyckoff_lines(payload),
        *_fallback_observation_lines(market, payload.get("sector_stats") or []),
        *_fallback_risk_lines(payload.get("risk_flags") or []),
    ]
    return "\n".join(lines).strip() + "\n"


def _fallback_market_lines(market: dict[str, Any]) -> list[str]:
    return [
        "### 一、大盘结构",
        (
            f"- 市场处在{market.get('regime_label', market.get('regime', 'NEUTRAL'))}状态，"
            f"当日涨跌 {market.get('main_today_pct')}，近3日累计 {market.get('recent3_cum_pct')}。"
        ),
        (f"- 指数收在 {market.get('close')}，MA50 {market.get('ma50')}、MA200 {market.get('ma200')}。"),
        (
            f"- 市场广度 {market.get('breadth_ratio')}，广度变化 {market.get('breadth_delta')}；"
            f"量能为{market.get('volume_state')}，5/20量比 {market.get('volume_ratio')}。"
        ),
        "",
    ]


def _fallback_wyckoff_lines(payload: dict[str, Any]) -> list[str]:
    style = payload.get("style_stats") or {}
    trigger = payload.get("trigger_stats") or {}
    return [
        "### 二、威科夫解读",
        (f"- 趋势推进结构 {style.get('trend_count', 0)}，吸筹/测试结构 {style.get('accum_count', 0)}。"),
        (
            f"- 结构触发上，强势推进确认 {trigger.get('sos_count', 0)}，"
            f"下探回收测试 {trigger.get('spring_count', 0)}，"
            f"回踩不破测试 {trigger.get('lps_count', 0)}，"
            f"缩量回撤测试 {trigger.get('evr_count', 0)}。"
        ),
        "",
    ]


def _fallback_observation_lines(market: dict[str, Any], sectors: list[dict[str, Any]]) -> list[str]:
    lines = ["### 三、观察要点"]
    if sectors:
        # 逐行「XX：结构出现聚集，热度中等」最多会占 5 行，而变量只有行业名和热度档，
        # 合成一行读起来更快，也省掉 4 遍重复的句式。
        clustered = "、".join(
            f"{item.get('industry')}（热度{_bucket_phrase(item.get('score_bucket'))}）" for item in sectors[:5]
        )
        lines.append(f"- 结构聚集行业：{clustered}。")
    else:
        lines.append("- 暂无明显行业聚集，结构分布偏分散。")
    lines.append(f"- 量价摘要：{market.get('pv_summary')}")
    lines.append(f"- 后续观察：{market.get('pv_outlook')}")
    lines.append("")
    return lines


def _fallback_risk_lines(risk_flags: list[str]) -> list[str]:
    lines = ["### 四、风险提示"]
    for item in risk_flags:
        lines.append(f"- {item}")
    # 三条免责声明每天一字不变，占正文全权重就是三行噪音。合成一句并以 ⚠️ 起头，
    # 让 build_report_card_elements 走 lark_note（灰色小字脚注）而不是正文 div。
    # 免责内容一条不少：不构成建议、可能有偏差、股市有风险。
    lines.append(_COMPLIANCE_DISCLAIMER)
    return lines


def _with_disclaimer(text: str) -> str:
    """
    给模型产出补免责声明。

    system prompt 从来没要求模型写免责声明，所以走 LLM 那条路的简报此前一条都没有 ——
    只有确定性模板里硬写着。声明由代码兜底而不是靠模型自觉：模型可能漏、可能改写，
    而这行是合规底线。已经写了就不重复追加。
    """
    body = (text or "").rstrip()
    if "不构成投资建议" in body:
        return body + "\n"
    return f"{body}\n\n{_COMPLIANCE_DISCLAIMER}\n"


def _compliance_user_message(payload: dict[str, Any]) -> str:
    return (
        "请根据以下脱敏市场指标生成合规版市场观察简报。"
        "用威科夫语气解释大盘结构强弱，不要使用任何个股代码、名称或交易动作词。\n\n" + _render_payload_text(payload)
    )


def generate_compliance_brief(
    *,
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str] | None = None,
    code_name: dict[str, str] | None = None,
    rag_veto_count: int = 0,
    llm_config: ComplianceLLMConfig | None = None,
    llm_caller: ComplianceLLMCaller | None = None,
) -> str:
    payload = build_public_payload(
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=ops_codes,
        rag_veto_count=rag_veto_count,
    )
    fallback = render_compliance_fallback(payload)
    if llm_config is None or llm_caller is None:
        return fallback

    forbidden_names = list((code_name or {}).values())
    retries = max(int(llm_config.retries), 0)
    max_output_tokens = max(int(llm_config.max_output_tokens), 512)
    user_message = _compliance_user_message(payload)
    last_reasons: tuple[str, ...] = ()
    for attempt in range(retries + 1):
        prompt = _system_prompt()
        if attempt > 0 and last_reasons:
            prompt += "\n上一版未通过合规校验，原因：" + "，".join(last_reasons) + "。请重写并严格避开。"
        try:
            text = llm_caller(
                provider=llm_config.provider,
                model=llm_config.model,
                api_key=llm_config.api_key,
                system_prompt=prompt,
                user_message=user_message,
                base_url=llm_config.base_url,
                timeout=llm_config.timeout_seconds,
                max_output_tokens=max_output_tokens,
            ).strip()
        except Exception as exc:
            logger.warning("[step3][compliance] %s 生成失败: %s", llm_config.source, exc)
            return fallback
        validation = validate_compliance_report(
            text,
            forbidden_names=forbidden_names,
            expected_trade_date=str(payload.get("trade_date") or ""),
        )
        if validation.ok:
            logger.info("[step3][compliance] 使用 %s 模型生成合规简报: %s", llm_config.source, llm_config.model)
            return _with_disclaimer(text)
        last_reasons = validation.reasons
        logger.warning(
            "[step3][compliance] 合规校验失败: attempt=%s/%s, reasons=%s",
            attempt + 1,
            retries + 1,
            ",".join(last_reasons),
        )

    logger.info("[step3][compliance] 已降级为确定性模板")
    return fallback
