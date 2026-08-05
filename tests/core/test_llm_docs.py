from __future__ import annotations

import json
from datetime import date

from core.llm_docs import build_llm_doc_context


def _write_index(tmp_path, documents):
    (tmp_path / "index.json").write_text(json.dumps({"documents": documents}), encoding="utf-8")


def test_build_llm_doc_context_selects_global_and_symbol_document(tmp_path):
    (tmp_path / "global.md").write_text("全局退出纪律", encoding="utf-8")
    (tmp_path / "case.md").write_text("大众交通案例", encoding="utf-8")
    _write_index(
        tmp_path,
        [
            {"id": "global", "path": "global.md", "scopes": ["step4"], "symbols": [], "priority": 1},
            {"id": "case", "path": "case.md", "scopes": ["step4"], "symbols": ["600611"], "priority": 2},
        ],
    )

    context = build_llm_doc_context("step4", symbols=["600611"], root=tmp_path)

    assert "大众交通案例" in context
    assert "全局退出纪律" in context
    assert context.index("大众交通案例") < context.index("全局退出纪律")
    assert "不得覆盖实时数据、硬止损、市场闸门、OMS" in context
    assert context.endswith("[LLM决策注释结束]")


def test_build_llm_doc_context_skips_wrong_symbol_expired_and_unsafe_path(tmp_path):
    (tmp_path / "case.md").write_text("不应出现", encoding="utf-8")
    _write_index(
        tmp_path,
        [
            {
                "id": "wrong-symbol",
                "path": "case.md",
                "scopes": ["step4"],
                "symbols": ["600611"],
            },
            {
                "id": "expired",
                "path": "case.md",
                "scopes": ["step4"],
                "symbols": [],
                "expires_on": "2026-08-01",
            },
            {
                "id": "future",
                "path": "case.md",
                "scopes": ["step4"],
                "symbols": [],
                "effective_from": "2026-08-06",
            },
            {"id": "unsafe", "path": "../README.md", "scopes": ["step4"], "symbols": []},
        ],
    )

    context = build_llm_doc_context(
        "step4",
        symbols=["600000"],
        as_of=date(2026, 8, 5),
        root=tmp_path,
    )

    assert context == ""


def test_build_llm_doc_context_keeps_expires_on_inclusive(tmp_path):
    (tmp_path / "case.md").write_text("到期日仍注入", encoding="utf-8")
    _write_index(
        tmp_path,
        [
            {
                "id": "edge",
                "path": "case.md",
                "scopes": ["step4"],
                "symbols": [],
                "expires_on": "2026-08-05",
            }
        ],
    )

    context = build_llm_doc_context("step4", as_of=date(2026, 8, 5), root=tmp_path)

    assert "到期日仍注入" in context


def test_build_llm_doc_context_fail_closed_on_disabled_and_invalid_effective_from(tmp_path):
    (tmp_path / "case.md").write_text("不应出现", encoding="utf-8")
    _write_index(
        tmp_path,
        [
            {
                "id": "disabled",
                "path": "case.md",
                "scopes": ["step4"],
                "symbols": [],
                "enabled": False,
            },
            {
                "id": "bad-date",
                "path": "case.md",
                "scopes": ["step4"],
                "symbols": [],
                "effective_from": "not-a-date",
            },
        ],
    )

    assert build_llm_doc_context("step4", root=tmp_path, as_of=date(2026, 8, 5)) == ""


def test_build_llm_doc_context_drops_whole_docs_instead_of_mid_cut(tmp_path):
    (tmp_path / "high.md").write_text("高优先级" + ("甲" * 400), encoding="utf-8")
    (tmp_path / "low.md").write_text("低优先级短文", encoding="utf-8")
    _write_index(
        tmp_path,
        [
            {"id": "high", "path": "high.md", "scopes": ["step4"], "symbols": [], "priority": 2},
            {"id": "low", "path": "low.md", "scopes": ["step4"], "symbols": [], "priority": 1},
        ],
    )

    context = build_llm_doc_context("step4", root=tmp_path, max_chars=220)

    assert "高优先级" not in context
    assert "低优先级短文" in context
    assert "不得覆盖实时数据、硬止损、市场闸门、OMS" in context
    assert context.endswith("[LLM决策注释结束]")
    assert "</document>" in context
    assert len(context) <= 220
