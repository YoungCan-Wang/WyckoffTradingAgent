from __future__ import annotations

from cli.context_layers import default_skill_docs, render_layered_docs


def test_unrelated_query_stays_at_abstract():
    text = render_layered_docs(default_skill_docs(), "天气怎么样", budget_chars=80)
    assert "[L0 funnel]" in text
    assert "OMS 买入还要过" not in text


def test_matching_query_can_promote_layer():
    text = render_layered_docs(default_skill_docs(), "漏斗 L4 选股", budget_chars=400)
    assert "[L1 funnel]" in text or "[L2 funnel]" in text
