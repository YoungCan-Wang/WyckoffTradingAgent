"""测试 TradingAgents 精选池注入逻辑（单元级，mock 重依赖）。"""

from __future__ import annotations

import os
from unittest.mock import patch

# ── 测试 1: 环境变量解析 ──────────────────────────────────────────


class TestEnvVarParsing:
    """测试 FUNNEL_EXTRA_* 环境变量解析。"""

    def test_not_set_defaults_disabled(self):
        """未设置时 FUNNEL_EXTRA_ENABLED=False。"""
        for k in [
            "FUNNEL_EXTRA_SYMBOLS",
            "FUNNEL_EXTRA_BONUS_SCORE",
            "FUNNEL_EXTRA_TRACK_TTL_DAYS",
            "FUNNEL_EXTRA_MAX_WATCH",
        ]:
            os.environ.pop(k, None)

        with patch("scripts.wyckoff_funnel.os.getenv") as mock_getenv:

            def _side_effect(key, default=""):
                env = {
                    "FUNNEL_EXTRA_SYMBOLS": "",
                    "FUNNEL_EXTRA_BONUS_SCORE": "15.0",
                    "FUNNEL_EXTRA_TRACK_TTL_DAYS": "5",
                    "FUNNEL_EXTRA_MAX_WATCH": "30",
                }
                return env.get(key, default)

            mock_getenv.side_effect = _side_effect

            import importlib

            import scripts.wyckoff_funnel as wf

            importlib.reload(wf)

            assert wf.FUNNEL_EXTRA_ENABLED is False
            assert wf.FUNNEL_EXTRA_BONUS_SCORE == 15.0
            assert wf.FUNNEL_EXTRA_TRACK_TTL_DAYS == 5
            assert wf.FUNNEL_EXTRA_MAX_WATCH == 30

    def test_comma_separated_enables(self):
        """逗号分隔代码 → FUNNEL_EXTRA_ENABLED=True。"""
        with patch("scripts.wyckoff_funnel.os.getenv") as mock_getenv:

            def _side_effect(key, default=""):
                env = {
                    "FUNNEL_EXTRA_SYMBOLS": "000001,600519,300750",
                    "FUNNEL_EXTRA_BONUS_SCORE": "15.0",
                    "FUNNEL_EXTRA_TRACK_TTL_DAYS": "5",
                    "FUNNEL_EXTRA_MAX_WATCH": "30",
                }
                return env.get(key, default)

            mock_getenv.side_effect = _side_effect

            import importlib

            import scripts.wyckoff_funnel as wf

            importlib.reload(wf)

            assert wf.FUNNEL_EXTRA_ENABLED is True
            assert wf.FUNNEL_EXTRA_SYMBOLS_RAW == "000001,600519,300750"

    def test_semicolon_separated(self):
        """分号分隔代码也可解析。"""
        with patch("scripts.wyckoff_funnel.os.getenv") as mock_getenv:

            def _side_effect(key, default=""):
                env = {
                    "FUNNEL_EXTRA_SYMBOLS": "000001;600519;300750",
                    "FUNNEL_EXTRA_BONUS_SCORE": "15.0",
                    "FUNNEL_EXTRA_TRACK_TTL_DAYS": "5",
                    "FUNNEL_EXTRA_MAX_WATCH": "30",
                }
                return env.get(key, default)

            mock_getenv.side_effect = _side_effect

            import importlib

            import scripts.wyckoff_funnel as wf

            importlib.reload(wf)

            assert wf.FUNNEL_EXTRA_ENABLED is True
            assert wf.FUNNEL_EXTRA_SYMBOLS_RAW == "000001;600519;300750"


# ── 测试 2: 注入逻辑（mock _normalize_symbols + 简化函数） ─────────


class TestInjectionLogic:
    """测试 TradingAgents 注入 run_funnel_job 的核心逻辑。"""

    def _simulate_injection(
        self, extra_raw: str, l1_passed: list[str], l2_passed: list[str], bypass_triggers: dict | None = None
    ):
        """模拟 run_funnel_job 中的注入逻辑，返回注入结果。"""
        parsed = [c.strip() for c in extra_raw.replace(";", ",").split(",") if c.strip()]
        l1_set = set(l1_passed)
        trading_agents_extra_codes = parsed
        trading_agents_extra_in_l1 = [c for c in parsed if c in l1_set]

        l2_rejected = [s for s in l1_passed if s not in set(l2_passed)]

        # 注入 L4 检测
        l2_bypass_in_sector = []  # 简化：模拟无热门板块
        if trading_agents_extra_in_l1:
            already_in = set(l2_bypass_in_sector)
            for c in trading_agents_extra_in_l1:
                if c not in already_in and c in l2_rejected:
                    l2_bypass_in_sector.append(c)

        bt = bypass_triggers or {}

        # 构建 bypass_pool（简化版）
        l2_bypass_pool = []
        for hits in bt.values():
            for code, _ in hits:
                if code not in l2_bypass_pool:
                    l2_bypass_pool.append(code)

        # 哨兵追踪
        trading_agents_extra_l4_triggers: dict[str, list[tuple[str, float]]] = {}
        trading_agents_extra_watch: list[str] = []
        if trading_agents_extra_in_l1:
            ta_hit_set: set[str] = set()
            extra_in_l1_set = set(trading_agents_extra_in_l1)
            for key, hits in bt.items():
                for code, score in hits:
                    if code in extra_in_l1_set:
                        ta_hit_set.add(code)
                        trading_agents_extra_l4_triggers.setdefault(key, []).append((code, score))
            l2_bypass_pool_set = set(l2_bypass_pool)
            for code in sorted(ta_hit_set):
                if code not in l2_bypass_pool_set:
                    l2_bypass_pool.append(code)
            trading_agents_extra_watch = [c for c in trading_agents_extra_in_l1 if c not in ta_hit_set]

        return {
            "trading_agents_extra_codes": trading_agents_extra_codes,
            "trading_agents_extra_in_l1": trading_agents_extra_in_l1,
            "l2_bypass_in_sector": l2_bypass_in_sector,
            "l2_bypass_pool": l2_bypass_pool,
            "trading_agents_extra_l4_triggers": trading_agents_extra_l4_triggers,
            "trading_agents_extra_watch": trading_agents_extra_watch,
        }

    def test_empty_pool_no_injection(self):
        """没有设置 FUNNEL_EXTRA_SYMBOLS → 无注入。"""
        result = self._simulate_injection("", ["000001", "600519"], ["000001"])
        assert result["trading_agents_extra_codes"] == []
        assert result["trading_agents_extra_in_l1"] == []
        assert result["l2_bypass_in_sector"] == []
        assert result["l2_bypass_pool"] == []

    def test_l1_filtering(self):
        """代码不在 L1_pass 列表里 → 被过滤。"""
        result = self._simulate_injection(
            "000001,600519,999999",
            ["000001", "600519"],
            ["000001"],
        )
        assert result["trading_agents_extra_codes"] == ["000001", "600519", "999999"]
        assert result["trading_agents_extra_in_l1"] == ["000001", "600519"]
        assert "999999" not in result["trading_agents_extra_in_l1"]

    def test_injection_into_l2_bypass(self):
        """L2 被拒的精选池标的→注入 l2_bypass_in_sector 做 L4 检测。"""
        result = self._simulate_injection(
            "000001,600519",
            ["000001", "600519", "300750"],
            ["000001"],
        )
        assert "600519" in result["l2_bypass_in_sector"]
        assert "000001" not in result["l2_bypass_in_sector"]

    def test_l4_triggers_promoted_to_bypass_pool(self):
        """有 L4 触发的精选池标的 → 加入 bypass_pool。"""
        result = self._simulate_injection(
            "000001,600519",
            ["000001", "600519"],
            [],
            bypass_triggers={
                "SOS": [("600519", 8.5)],
                "SPRING": [("300750", 6.0)],
            },
        )
        assert "600519" in result["l2_bypass_pool"]
        assert "000001" not in result["l2_bypass_pool"]

    def test_no_l4_triggers_go_to_watch(self):
        """无 L4 触发的精选池标的 → 进哨兵追踪。"""
        result = self._simulate_injection(
            "000001,600519",
            ["000001", "600519"],
            [],
            bypass_triggers={
                "SOS": [("600519", 8.5)],
            },
        )
        assert "600519" not in result["trading_agents_extra_watch"]
        assert "000001" in result["trading_agents_extra_watch"]
        assert "SOS" in result["trading_agents_extra_l4_triggers"]
        codes_in_triggers = {c for v in result["trading_agents_extra_l4_triggers"].values() for c, _ in v}
        assert "600519" in codes_in_triggers
        assert "000001" not in codes_in_triggers


# ── 测试 3: 评分加成逻辑 ──────────────────────────────────────────


class TestScoreBonus:
    """测试 run() 中的 TradingAgents 评分加成。"""

    def test_bonus_score_applied(self):
        """精选池标的获得 FUNNEL_EXTRA_BONUS_SCORE 基础分。"""
        code_to_total_score: dict[str, float] = {}
        code_to_reasons: dict[str, list[str]] = {}
        code_to_trigger_keys: dict[str, list[str]] = {}

        trading_agents_extra_codes = ["000001", "600519"]
        extra_bonus = 15.0

        for code in trading_agents_extra_codes:
            code_to_reasons.setdefault(code, [])
            code_to_trigger_keys.setdefault(code, [])
            if code not in code_to_total_score or code_to_total_score[code] == 0.0:
                code_to_total_score[code] = extra_bonus
            if "TradingAgents" not in code_to_reasons[code]:
                code_to_reasons[code].append("TradingAgents")

        assert code_to_total_score["000001"] == 15.0
        assert code_to_total_score["600519"] == 15.0
        assert "TradingAgents" in code_to_reasons["000001"]
        assert "TradingAgents" in code_to_reasons["600519"]

    def test_does_not_override_existing_score(self):
        """已有 L4 触发分数的标的，满分不加倍（只补 0 分）。"""
        code_to_total_score: dict[str, float] = {"600519": 28.5}
        code_to_reasons: dict[str, list[str]] = {"600519": ["SOS"]}
        code_to_trigger_keys: dict[str, list[str]] = {"600519": ["SOS"]}

        trading_agents_extra_codes = ["000001", "600519"]
        extra_bonus = 15.0

        for code in trading_agents_extra_codes:
            code_to_reasons.setdefault(code, [])
            code_to_trigger_keys.setdefault(code, [])
            if code not in code_to_total_score or code_to_total_score[code] == 0.0:
                code_to_total_score[code] = extra_bonus
            if "TradingAgents" not in code_to_reasons[code]:
                code_to_reasons[code].append("TradingAgents")

        assert code_to_total_score["600519"] == 28.5
        assert code_to_total_score["000001"] == 15.0
        assert "TradingAgents" in code_to_reasons["600519"]
        assert "SOS" in code_to_reasons["600519"]


# ── 测试 4: signal_pending 写入逻辑（daily_job）────────────────────


class TestSignalPendingInjection:
    """测试 daily_job _run_signal_confirmation 中的 TA_WATCH 写入。"""

    def _simulate_ta_watch_injection(self, step2_details: dict) -> dict:
        """模拟 daily_job 里的 TA_WATCH 注入逻辑。"""
        triggers_raw = dict(step2_details.get("triggers", {}))
        _metrics = step2_details.get("metrics", {}) or {}
        trading_agents_extra_watch = _metrics.get("trading_agents_extra_watch", []) or []

        if trading_agents_extra_watch:
            watch_scores = [(c, 0.0) for c in trading_agents_extra_watch]
            triggers_raw.setdefault("TA_WATCH", [])
            existing_ta = {c for c, _ in triggers_raw["TA_WATCH"]}
            for c, s in watch_scores:
                if c not in existing_ta:
                    triggers_raw["TA_WATCH"].append((c, s))
                    existing_ta.add(c)

        return triggers_raw

    def test_watch_codes_injected_as_ta_watch(self):
        """trading_agents_extra_watch → triggers_raw 里出现 TA_WATCH 键。"""
        step2_details = {
            "triggers": {"SOS": [("600519", 8.5)]},
            "all_df_map": {},
            "metrics": {
                "trading_agents_extra_watch": ["000001"],
            },
        }
        result = self._simulate_ta_watch_injection(step2_details)
        assert "TA_WATCH" in result
        assert ("000001", 0.0) in result["TA_WATCH"]
        assert ("600519", 8.5) in result["SOS"]

    def test_empty_watch_noop(self):
        """watch 列表为空 → 不注入 TA_WATCH。"""
        step2_details = {
            "triggers": {"SOS": [("600519", 8.5)]},
            "all_df_map": {},
            "metrics": {},
        }
        result = self._simulate_ta_watch_injection(step2_details)
        assert "TA_WATCH" not in result

    def test_dedup_with_existing_ta_watch(self):
        """已存在 TA_WATCH 里的代码不被重复添加。"""
        step2_details = {
            "triggers": {"TA_WATCH": [("000001", 0.0)]},
            "all_df_map": {},
            "metrics": {
                "trading_agents_extra_watch": ["000001", "600519"],
            },
        }
        result = self._simulate_ta_watch_injection(step2_details)
        ta_watch_codes = [c for c, _ in result.get("TA_WATCH", [])]
        assert ta_watch_codes == ["000001", "600519"]
