"""workflows/candidate_policy_config.py 的环境变量装配测试。"""

from __future__ import annotations

from workflows.candidate_policy_config import candidate_policy_config_from_env


class TestCandidatePolicyConfigFromEnv:
    def test_defaults_match_dataclass_defaults(self, monkeypatch):
        for key in (
            "FUNNEL_LOSS_GUARD_WEAK_CONFIRMATION_MIN_ABC",
            "FUNNEL_LOSS_GUARD_PURE_SOS_MIN_ABC",
        ):
            monkeypatch.delenv(key, raising=False)
        cfg = candidate_policy_config_from_env()
        assert cfg.weak_confirmation_min_abc == 2
        assert cfg.pure_sos_min_abc == 3

    def test_pure_sos_min_abc_can_be_rolled_back_via_env(self, monkeypatch):
        """实盘出现误拦截时，运维应能通过环境变量一键回退到旧门槛(2)而无需改代码重新部署。"""
        monkeypatch.setenv("FUNNEL_LOSS_GUARD_PURE_SOS_MIN_ABC", "2")
        cfg = candidate_policy_config_from_env()
        assert cfg.pure_sos_min_abc == 2

    def test_weak_confirmation_min_abc_overridable(self, monkeypatch):
        monkeypatch.setenv("FUNNEL_LOSS_GUARD_WEAK_CONFIRMATION_MIN_ABC", "1")
        cfg = candidate_policy_config_from_env()
        assert cfg.weak_confirmation_min_abc == 1

    def test_invalid_env_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("FUNNEL_LOSS_GUARD_PURE_SOS_MIN_ABC", "not-a-number")
        cfg = candidate_policy_config_from_env()
        assert cfg.pure_sos_min_abc == 3
