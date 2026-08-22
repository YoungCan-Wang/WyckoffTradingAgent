"""L0/L1/L2 agent context loading — abstract, overview, then full text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Layer = Literal["L0", "L1", "L2"]


@dataclass(frozen=True)
class ContextDoc:
    doc_id: str
    title: str
    abstract: str
    overview: str
    body: str
    keywords: tuple[str, ...] = ()


def layer_text(doc: ContextDoc, layer: Layer) -> str:
    if layer == "L0":
        return doc.abstract
    if layer == "L1":
        return doc.overview
    return doc.body


def choose_layer(doc: ContextDoc, query: str, *, budget_chars: int) -> Layer:
    if _query_hits(doc, query) and budget_chars >= len(doc.body):
        return "L2"
    if _query_hits(doc, query) and budget_chars >= len(doc.overview):
        return "L1"
    return "L0"


def render_layered_docs(docs: list[ContextDoc], query: str, *, budget_chars: int = 1200) -> str:
    lines: list[str] = []
    remaining = budget_chars
    for doc in docs:
        layer = choose_layer(doc, query, budget_chars=remaining)
        text = layer_text(doc, layer)
        block = f"[{layer} {doc.doc_id}] {doc.title}: {text}"
        if len(block) > remaining:
            break
        lines.append(block)
        remaining -= len(block) + 1
    return "\n".join(lines)


def default_skill_docs() -> list[ContextDoc]:
    return [
        ContextDoc(
            doc_id="funnel",
            title="漏斗分层",
            abstract="先环境后选股，L1 到 L4 是硬门控。",
            overview="L1 流动性/财务准入，L2 八通道，L3 板块共振，L4 威科夫触发。confirmed 仍不等于可买。",
            body="漏斗输出只进入观察与 Step3 参谋。OMS 买入还要过水温闸门、跨日确认和唯一允许买入区间。",
            keywords=("漏斗", "funnel", "L4", "选股"),
        ),
        ContextDoc(
            doc_id="oms",
            title="OMS 纪律",
            abstract="EXIT 优先于买入；AI 不能升级交易许可。",
            overview="优先级 EXIT>TRIM>HOLD>PROBE/ATTACK。生产硬止损 floor 为 5%。",
            body="NEUTRAL 与 RISK_ON 默认禁买，BEAR_REBOUND 才是当前唯一允许新开的水温。",
            keywords=("oms", "止损", "买入", "闸门"),
        ),
    ]


def _query_hits(doc: ContextDoc, query: str) -> bool:
    blob = " ".join((doc.doc_id, doc.title, *doc.keywords, doc.abstract)).lower()
    tokens = [token for token in query.lower().replace("，", " ").split() if token]
    return any(token in blob for token in tokens)
