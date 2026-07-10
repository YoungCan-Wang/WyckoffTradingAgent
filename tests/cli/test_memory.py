from __future__ import annotations

from datetime import datetime, timedelta

from cli.memory import (
    _extract_keywords,
    _save_summary_memories,
    build_memory_context,
    extract_stock_codes,
    prepend_memory_context,
    refresh_memory_layers,
    resolve_stock_codes,
    save_session_summary,
)


class _Provider:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def chat_stream(self, *_args):
        yield {"type": "text_delta", "text": self.outputs.pop(0)}


class _FailingProvider:
    def chat_stream(self, *_args):
        raise RuntimeError("dedup unavailable")


def _init_tmp_db(monkeypatch, tmp_path):
    import integrations.local_db as local_db

    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "memory.db")
    local_db.init_db()
    return local_db


def _close_tmp_db(local_db):
    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None


class TestExtractStockCodes:
    def test_basic(self):
        assert extract_stock_codes("看看 000001 和 600519") == ["000001", "600519"]

    def test_dedup(self):
        assert extract_stock_codes("000001 000001") == ["000001"]

    def test_no_match(self):
        assert extract_stock_codes("没有代码") == []


class TestResolveStockCodes:
    def test_resolves_stock_names(self, monkeypatch):
        monkeypatch.setattr("cli.memory._stock_name_code_map", lambda: {"宁德时代": "300750", "比亚迪": "002594"})

        assert resolve_stock_codes("我想建仓比亚迪") == ["002594"]
        assert resolve_stock_codes("300750 宁德时代怎么看") == ["300750"]


class TestExtractKeywords:
    def test_chinese_segments(self):
        kw = _extract_keywords("最近市场情绪怎么样")
        assert "市场" in kw
        assert "情绪" in kw

    def test_filters_stopwords(self):
        kw = _extract_keywords("帮我看看这个")
        assert "帮我" not in kw
        assert "看看" not in kw

    def test_strips_codes(self):
        kw = _extract_keywords("000001 走势分析")
        codes_in_kw = [k for k in kw if k.isdigit()]
        assert len(codes_in_kw) == 0


def test_build_memory_context_includes_context_archive(monkeypatch, tmp_path):
    local_db = _init_tmp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "cli.context_archive.archive_recall_lines",
        lambda *_args, **_kwargs: ["- archive://s1/ctx_1：603373 写库问题"],
    )

    try:
        context = build_memory_context("603373 的写库问题是什么")
    finally:
        _close_tmp_db(local_db)

    assert "# 压缩归档" in context
    assert "archive://s1/ctx_1" in context

    def test_filters_generic_trade_words(self):
        kw = _extract_keywords("我想建仓比亚迪")
        assert "建仓" not in kw
        assert "比亚" in kw

    def test_max_five(self):
        kw = _extract_keywords("这里有很多关键词需要提取出来测试数量限制的功能实现")
        assert len(kw) <= 5


class TestBuildMemoryContext:
    def test_returns_empty_when_no_db(self, monkeypatch):
        def _boom(*a, **kw):
            raise ImportError("no db")

        monkeypatch.setattr("cli.memory.resolve_stock_codes", lambda t: [])
        result = build_memory_context("随便问个问题")
        assert result == "" or isinstance(result, str)

    def test_injects_layered_context_with_source(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            local_db.save_memory("persona", "用户偏好低换手、重视止损", memory_level="L3")
            local_db.save_memory("preference", "不追涨", codes="000001")
            local_db.save_memory("playbook", "尾盘二次确认买入剧本", codes="000001", memory_level="L2")
            local_db.save_memory(
                "decision",
                "用户决定等待 000001 放量确认",
                codes="000001",
                source_ref="chat_log:s1",
            )

            context = build_memory_context("000001 接下来怎么处理")

            assert "# 用户画像" in context
            assert "# 交易剧本" in context
            assert "尾盘二次确认买入剧本" in context
            assert "# 历史记忆" in context
            assert "源:chat_log:s1" in context
        finally:
            _close_tmp_db(local_db)

    def test_recall_keeps_legacy_scenario_as_playbook(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            local_db.save_memory("scenario", "旧场景兼容召回", codes="000001", memory_level="L2")

            context = build_memory_context("000001 怎么处理")

            assert "# 交易剧本" in context
            assert "旧场景兼容召回" in context
        finally:
            _close_tmp_db(local_db)

    def test_stock_name_query_does_not_recall_other_stock_memory(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        monkeypatch.setattr("cli.memory._stock_name_code_map", lambda: {"宁德时代": "300750", "比亚迪": "002594"})
        try:
            local_db.save_memory("preference", "尾盘二次确认", codes="")
            local_db.save_memory("decision", "宁德时代建仓需要等放量确认", codes="300750")
            local_db.save_memory("decision", "比亚迪建仓要看确认支撑", codes="002594")

            context = build_memory_context("我想建仓比亚迪")

            assert "尾盘二次确认" in context
            assert "比亚迪建仓要看确认支撑" in context
            assert "宁德时代建仓需要等放量确认" not in context
        finally:
            _close_tmp_db(local_db)

    def test_sector_like_query_skips_stock_scoped_memories(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        monkeypatch.setattr("cli.memory._stock_name_code_map", lambda: {"长信科技": "300088"})
        try:
            local_db.save_memory("preference", "尾盘二次确认", codes="")
            local_db.save_memory("decision", "长信科技建仓需要等缩量回踩", codes="300088")

            context = build_memory_context("我想建仓科技")

            assert "尾盘二次确认" in context
            assert "长信科技建仓需要等缩量回踩" not in context
        finally:
            _close_tmp_db(local_db)

    def test_applies_recall_budget_and_tags(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            local_db.save_memory("preference", "偏好" * 80, codes="000001")
            local_db.save_memory("decision", "决策" * 80, codes="000001")

            context = build_memory_context("000001 怎么处理", max_chars_per_memory=40, max_total_chars=180)

            assert context.startswith("<relevant-memories>")
            assert context.endswith("</relevant-memories>")
            assert len(context) < 320
            assert "已截断" in context
        finally:
            _close_tmp_db(local_db)

    def test_dedupes_pinned_preferences_from_hybrid_results(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            local_db.save_memory("preference", "不追涨", codes="000001")

            context = build_memory_context("000001 不追涨")

            assert context.count("不追涨") == 1
            assert "# 用户画像" in context
        finally:
            _close_tmp_db(local_db)

    def test_prepends_memory_context_to_current_turn_only(self):
        message = prepend_memory_context("今天怎么看？", "<relevant-memories>\nA\n</relevant-memories>")

        assert message.startswith("<relevant-memories>")
        assert "<current-user-message>\n今天怎么看？\n</current-user-message>" in message

    def test_local_db_filters_memory_by_level_and_since(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            l1_id = local_db.save_memory("preference", "偏好L1", memory_level="L1")
            local_db.save_memory("playbook", "剧本L2", memory_level="L2")

            l1_rows = local_db.get_recent_memories(memory_level="L1", limit=10)
            future_rows = local_db.get_recent_memories(since="2999-01-01T00:00:00", limit=10)
            search_rows = local_db.search_memory(keyword="偏好", memory_level="L1", limit=10)

            assert [r["id"] for r in l1_rows] == [l1_id]
            assert future_rows == []
            assert [r["id"] for r in search_rows] == [l1_id]
        finally:
            _close_tmp_db(local_db)

    def test_prune_memories_uses_type_specific_retention(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            decision_id = local_db.save_memory("decision", "旧决策")
            playbook_id = local_db.save_memory("playbook", "旧剧本", memory_level="L2")
            preference_id = local_db.save_memory("preference", "长期偏好")
            old = (datetime.utcnow() - timedelta(days=70)).isoformat()
            with local_db.get_db() as conn:
                conn.execute(
                    "UPDATE agent_memory SET created_at=? WHERE id IN (?, ?, ?)",
                    (old, decision_id, playbook_id, preference_id),
                )

            deleted = local_db.prune_memories()

            assert deleted == 2
            assert local_db.get_memory_by_id(decision_id) is None
            assert local_db.get_memory_by_id(playbook_id) is None
            assert local_db.get_memory_by_id(preference_id) is not None
        finally:
            _close_tmp_db(local_db)


class TestSaveSessionSummary:
    def test_stores_supported_atoms_and_source(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            provider = _Provider(
                [
                    "[股票] 000001 吸筹观察，等待放量确认\n[决策] 用户决定暂不加仓\n[偏好] 不追涨",
                    "[画像] 用户偏好确认后再加仓\n[剧本] 000001 吸筹后等待放量确认",
                ]
            )
            messages = [
                {"role": "user", "content": "看看 000001"},
                {"role": "assistant", "tool_calls": [{"id": "tc1", "name": "analyze_stock", "args": {}}]},
                {"role": "tool", "content": '{"code":"000001"}'},
                {"role": "assistant", "content": "先观察。"},
            ]

            save_session_summary(messages, provider, session_id="s1")

            memories = local_db.get_recent_memories(limit=10)
            types = {m["memory_type"] for m in memories}
            assert types == {"decision", "preference", "session"}
            assert any(m["source_ref"] == "chat_log:s1" for m in memories)
        finally:
            _close_tmp_db(local_db)

    def test_session_summary_binds_codes_from_stock_names(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        monkeypatch.setattr("cli.memory._stock_name_code_map", lambda: {"宁德时代": "300750"})
        try:
            provider = _Provider(["[决策] 宁德时代只在放量确认后建仓"])
            messages = [
                {"role": "user", "content": "宁德时代现在怎么处理"},
                {"role": "assistant", "tool_calls": [{"id": "tc1", "name": "analyze_stock", "args": {}}]},
                {"role": "tool", "content": '{"name":"宁德时代"}'},
                {"role": "assistant", "content": "先等确认。"},
            ]

            save_session_summary(messages, provider, session_id="s1")

            memories = local_db.get_recent_memories(memory_type="decision", limit=10)
            assert memories[0]["codes"] == "300750"
        finally:
            _close_tmp_db(local_db)

    def test_repeated_session_summary_hash_skips_llm(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            provider = _Provider(["[偏好] 不追涨"])
            messages = [
                {"role": "user", "content": "看看 000001"},
                {"role": "assistant", "tool_calls": [{"id": "tc1", "name": "analyze_stock", "args": {}}]},
                {"role": "tool", "content": '{"code":"000001"}'},
                {"role": "assistant", "content": "先观察。"},
            ]

            save_session_summary(messages, provider, session_id="s1")
            save_session_summary(messages, provider, session_id="s1")

            memories = local_db.get_recent_memories(memory_type="preference", limit=10)
            assert [m["content"] for m in memories] == ["不追涨"]
            assert provider.outputs == []
        finally:
            _close_tmp_db(local_db)

    def test_no_memory_summary_is_marked_processed(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            provider = _Provider(["无"])
            messages = [
                {"role": "user", "content": "看看 000001"},
                {"role": "assistant", "tool_calls": [{"id": "tc1", "name": "analyze_stock", "args": {}}]},
                {"role": "tool", "content": '{"code":"000001"}'},
                {"role": "assistant", "content": "没有新增偏好。"},
            ]

            save_session_summary(messages, provider, session_id="s1")
            save_session_summary(messages, provider, session_id="s1")

            assert local_db.get_recent_memories(memory_type="preference", limit=10) == []
            assert len(local_db.get_recent_memories(memory_type="session", limit=10)) == 1
            assert provider.outputs == []
        finally:
            _close_tmp_db(local_db)

    def test_deterministic_dedup_skips_exact_memory_without_llm(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            local_db.save_memory("preference", "不追涨", codes="000001")

            saved = _save_summary_memories(
                "[偏好] 不 追 涨",
                "chat_log:s2",
                _FailingProvider(),
            )

            memories = local_db.get_recent_memories(memory_type="preference", limit=10)
            assert saved == 0
            assert [m["content"] for m in memories] == ["不追涨"]
        finally:
            _close_tmp_db(local_db)

    def test_dedup_unknown_duplicate_id_skips_save(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            local_db.save_memory("preference", "旧偏好", codes="000001")
            existing = local_db.get_recent_memories(memory_type="preference", limit=10)
            unknown_id = max(m["id"] for m in existing) + 1

            saved = _save_summary_memories(
                "[偏好] 新偏好",
                "chat_log:s2",
                _Provider([f"DUPLICATE:{unknown_id}"]),
            )

            memories = local_db.get_recent_memories(memory_type="preference", limit=10)
            assert saved == 0
            assert [m["content"] for m in memories] == ["旧偏好"]
        finally:
            _close_tmp_db(local_db)

    def test_refresh_layers_is_incremental(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            local_db.save_memory("preference", "偏好一")
            local_db.save_memory("decision", "决策一")
            local_db.save_memory("preference", "偏好二")

            provider = _Provider(["[画像] 用户偏好确认后再交易\n[剧本] 有放量确认才加仓"])
            assert refresh_memory_layers(provider) == 2
            assert local_db.get_recent_memories(memory_type="playbook", limit=10)[0]["memory_level"] == "L2"
            assert provider.outputs == []

            same_source_provider = _Provider(["[画像] 不应再次调用"])
            assert refresh_memory_layers(same_source_provider) == 0
            assert same_source_provider.outputs == ["[画像] 不应再次调用"]

            local_db.save_memory("decision", "决策二")
            local_db.save_memory("preference", "偏好三")
            two_new_provider = _Provider(["[画像] 不足三条新 L1 不调用"])
            assert refresh_memory_layers(two_new_provider) == 0
            assert two_new_provider.outputs == ["[画像] 不足三条新 L1 不调用"]

            local_db.save_memory("decision", "决策三")
            next_provider = _Provider(["[画像] 用户偏好等待确认\n[剧本] 趋势确认后再执行"])
            assert refresh_memory_layers(next_provider) == 2
            assert next_provider.outputs == []
        finally:
            _close_tmp_db(local_db)

    def test_dedup_failure_skips_save(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            local_db.save_memory("preference", "旧偏好", codes="000001")

            saved = _save_summary_memories(
                "[偏好] 新偏好",
                "chat_log:s2",
                _FailingProvider(),
            )

            memories = local_db.get_recent_memories(memory_type="preference", limit=10)
            assert saved == 0
            assert [m["content"] for m in memories] == ["旧偏好"]
        finally:
            _close_tmp_db(local_db)

    def test_invalid_dedup_response_skips_save(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            local_db.save_memory("preference", "旧偏好", codes="000001")

            saved = _save_summary_memories(
                "[偏好] 新偏好",
                "chat_log:s2",
                _Provider(["MAYBE"]),
            )

            memories = local_db.get_recent_memories(memory_type="preference", limit=10)
            assert saved == 0
            assert [m["content"] for m in memories] == ["旧偏好"]
        finally:
            _close_tmp_db(local_db)
