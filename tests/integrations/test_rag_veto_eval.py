"""RAG 防雷关键词提取 eval — 覆盖主语校验 + ST 精确匹配的各类边界场景。"""

from __future__ import annotations

import pytest

from integrations.rag_veto import _extract_hits_strict, _is_about_this_stock, _st_mentions_this_stock

KEYWORDS = ["立案", "调查", "证监会", "处罚", "违规", "造假", "退市", "st", "*st", "减持", "业绩预亏", "业绩下滑"]


class TestExtractHitsStrict:
    """非 ST 关键词：单股新闻源直接匹配，无需主语校验。"""

    def test_real_negative_no_stock_name(self):
        hits, _ = _extract_hits_strict(["公司被证监会立案调查"], KEYWORDS, "000001", "平安银行")
        assert "立案" in hits
        assert "调查" in hits
        assert "证监会" in hits

    def test_real_reduce_no_stock_name(self):
        hits, _ = _extract_hits_strict(["股东李某减持1.5%股份"], KEYWORDS, "002119", "康强电子")
        assert "减持" in hits

    def test_performance_warning(self):
        hits, _ = _extract_hits_strict(["公司发布业绩预亏公告"], KEYWORDS, "600001", "邯郸钢铁")
        assert "业绩预亏" in hits

    def test_no_keyword_no_hit(self):
        hits, _ = _extract_hits_strict(["公司发布正常经营公告"], KEYWORDS, "000001", "平安银行")
        assert hits == []

    def test_evidence_returns_title(self):
        _, ev = _extract_hits_strict(["重大事项公告\n公司被立案调查详情..."], KEYWORDS, "000001", "平安银行")
        assert "重大事项公告" in ev


class TestScanOneFailClosed:
    def test_keyword_hit_vetoes_when_semantic_config_missing(self, monkeypatch):
        from integrations import rag_veto as mod

        monkeypatch.setattr(mod, "RAG_SEMANTIC_VETO_ENABLED", True)
        monkeypatch.setattr(mod, "RAG_SEMANTIC_API_KEY", "")
        monkeypatch.setattr(mod, "RAG_SEMANTIC_MODEL", "")
        monkeypatch.setattr(mod, "RAG_SEMANTIC_BASE_URL", "")
        monkeypatch.setattr(mod, "RAG_SEMANTIC_PROVIDER", "")
        monkeypatch.setattr(
            mod,
            "_fetch_news_akshare",
            lambda _code: [{"title": "重大事项公告", "content": "公司被证监会立案调查"}],
        )

        result = mod._scan_one("000001", "平安银行", KEYWORDS)

        assert result.veto is True
        assert {"立案", "调查", "证监会"}.issubset(set(result.hits))
        assert result.semantic_checked is False
        assert result.error == "semantic_disabled:missing_RAG_SEMANTIC_*_config"

    def test_keyword_hit_uses_provider_fallback_for_semantic_check(self, monkeypatch):
        from integrations import llm_client
        from integrations import rag_veto as mod

        captured: dict[str, str] = {}

        def fake_call_llm(**kwargs):
            captured.update({k: str(v) for k, v in kwargs.items() if k in {"provider", "api_key", "model", "base_url"}})
            return '{"is_extreme_negative": false, "reason": "澄清公告"}'

        monkeypatch.setattr(mod, "RAG_SEMANTIC_VETO_ENABLED", True)
        monkeypatch.setattr(mod, "RAG_SEMANTIC_API_KEY", "")
        monkeypatch.setattr(mod, "RAG_SEMANTIC_MODEL", "")
        monkeypatch.setattr(mod, "RAG_SEMANTIC_BASE_URL", "")
        monkeypatch.setattr(mod, "RAG_SEMANTIC_PROVIDER", "efficiency")
        monkeypatch.setenv("EFFICIENCY_API_KEY", "eff-key")
        monkeypatch.setenv("EFFICIENCY_MODEL", "eff-model")
        monkeypatch.setenv("EFFICIENCY_BASE_URL", "https://llm.example/v1")
        monkeypatch.setattr(
            mod,
            "_fetch_news_akshare",
            lambda _code: [{"title": "澄清公告", "content": "公司澄清未被证监会立案调查"}],
        )
        monkeypatch.setattr(llm_client, "call_llm", fake_call_llm)

        result = mod._scan_one("000001", "平安银行", KEYWORDS)

        assert result.veto is False
        assert result.semantic_checked is True
        assert result.semantic_negative is False
        assert result.error is None
        assert captured == {
            "provider": "efficiency",
            "api_key": "eff-key",
            "model": "eff-model",
            "base_url": "https://llm.example/v1",
        }


class TestStMentionsThisStock:
    """ST 关键词精确匹配：只有 *ST/ST + 本股名称前缀才算命中。"""

    def test_stock_is_st(self):
        assert _st_mentions_this_stock("*st平安今日跌停", "000001", "平安银行") is True

    def test_stock_is_st_with_space(self):
        assert _st_mentions_this_stock("*st 平安将被退市", "000001", "平安银行") is True

    def test_other_st_stock(self):
        assert _st_mentions_this_stock("*st东园被调查", "000001", "平安银行") is False

    def test_other_st_in_mixed_article(self):
        text = "平安银行公告称业务正常。*st东园被调查。"
        assert _st_mentions_this_stock(text, "000001", "平安银行") is False

    def test_st_stock_self_name_starts_with_st(self):
        assert _st_mentions_this_stock("*st美格被处罚", "002881", "*ST美格") is True

    def test_empty_name_returns_false(self):
        assert _st_mentions_this_stock("*st某某跌停", "000001", "") is False


class TestMixedArticleScenarios:
    """混合文章场景：本股 + 其他 ST 股同时出现。"""

    def test_mixed_only_hits_non_st_keyword(self):
        articles = ["平安银行公告称业务正常。*ST东园被调查。"]
        hits, _ = _extract_hits_strict(articles, KEYWORDS, "000001", "平安银行")
        assert "调查" in hits
        assert "*st" not in hits
        assert "st" not in hits

    def test_aggregated_article_other_st_no_hit(self):
        articles = ["龙虎榜: *ST美利跌停, 比亚迪涨5%"]
        hits, _ = _extract_hits_strict(articles, KEYWORDS, "002594", "比亚迪")
        assert "*st" not in hits
        assert "st" not in hits

    def test_self_st_in_aggregated_hits(self):
        articles = ["龙虎榜: *ST比亚今日涨停"]
        hits, _ = _extract_hits_strict(articles, KEYWORDS, "002594", "比亚迪")
        assert "*st" in hits

    def test_multiple_articles_independent(self):
        articles = [
            "公司正常经营公告",
            "股东减持2%股份",
            "*ST东园被立案",
        ]
        hits, _ = _extract_hits_strict(articles, KEYWORDS, "600100", "同方股份")
        assert "减持" in hits
        assert "立案" in hits
        assert "*st" not in hits


class TestIsAboutThisStock:
    """_is_about_this_stock 辅助函数。"""

    def test_code_match(self):
        assert _is_about_this_stock("002119今日涨停", "002119", "康强电子") is True

    def test_name_match(self):
        assert _is_about_this_stock("康强电子发布公告", "002119", "康强电子") is True

    def test_st_name_cleaned(self):
        assert _is_about_this_stock("美格智能公告", "002881", "*ST美格") is True

    def test_no_match(self):
        assert _is_about_this_stock("某公司发布公告", "000001", "平安银行") is False


class TestRealWorldCases:
    """从实际误判案例回归。"""

    @pytest.mark.parametrize(
        "code,name,article,should_hit_st",
        [
            ("002881", "美格智能", "45只股上午收盘涨停(附股)\n...*ST东园涨停...", False),
            ("000759", "中百集团", "241只股短线走稳\n...*ST某某...", False),
            ("603075", "热威股份", "277只个股流通市值不足20亿元\n*ST小股跌停", False),
            ("002815", "崇达技术", "87股筹码连续3期集中\n*ST崇达被调查", True),
        ],
    )
    def test_st_false_positive_regression(self, code, name, article, should_hit_st):
        hits, _ = _extract_hits_strict([article], KEYWORDS, code, name)
        if should_hit_st:
            assert "*st" in hits or "st" in hits
        else:
            assert "*st" not in hits and "st" not in hits
