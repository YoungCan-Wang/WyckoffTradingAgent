"""Tests for factor IC evaluation.

生产漏斗全是阈值门（`rank(axis=1)` 在 core/ 零命中），导致门槛线上的票本质随机、
参数必然过拟合（walk-forward 1/16）、且只能过滤不能排序。IC 用横截面秩相关衡量
预测力，不需要切点。

2026-08-22 首轮：32 个因子-前瞻组合全为负 IC，其中 6 个通过可用门槛，
19 个在 3 段样本上方向全一致——这是当日唯一跨段稳定的结论。
用例守住「无方向性优先」与「稳定性重于幅度」两条判读原则。
"""

from __future__ import annotations

import pytest

from core.factor_ic import (
    MIN_DAYS,
    RANDOM_BAND,
    USEFUL_ABS_IC,
    USEFUL_ABS_IC_IR,
    FactorICResult,
    composite_weights,
    summarize_ic,
)


def _res(ic: float, std: float, pos: float, days: int = 200) -> FactorICResult:
    return FactorICResult("f", 5, days, ic, std, pos, 0.1, 3900.0)


class TestSummarize:
    def test_requires_min_days(self):
        r = summarize_ic("x", 5, [0.03] * (MIN_DAYS - 1), [3900] * (MIN_DAYS - 1))
        assert r.verdict == "样本不足"
        assert r.useful is False

    def test_drops_nan(self):
        vals = [0.03] * MIN_DAYS + [float("nan")] * 5
        assert summarize_ic("x", 5, vals, [3900] * len(vals)).days == MIN_DAYS

    def test_zero_std_gives_no_ir(self):
        """常数 IC 序列的 IR 无意义，必须返回 None 而非无穷。"""
        r = summarize_ic("x", 5, [0.03] * 200, [3900] * 200)
        assert r.ic_ir is None
        assert r.useful is False


class TestDirectionless:
    @pytest.mark.parametrize("pos", [RANDOM_BAND[0], 50.0, RANDOM_BAND[1]])
    def test_random_band_rejected_even_with_high_ic(self, pos):
        """为正日占比落在噪声带内，IC 再高也不采用。"""
        r = _res(0.09, 0.10, pos)
        assert r.directionless is True
        assert r.verdict == "无方向性"
        assert r.useful is False

    def test_outside_band_usable(self):
        assert _res(0.04, 0.10, 62.0).useful is True


class TestThresholds:
    def test_weak_ic_not_useful(self):
        r = _res(USEFUL_ABS_IC / 2, 0.02, 70.0)
        assert r.useful is False
        assert "偏弱" in r.verdict

    def test_weak_ir_not_useful(self):
        """IC 够大但不稳定——这类因子下不了注。"""
        r = _res(0.05, 0.05 / (USEFUL_ABS_IC_IR / 2), 70.0)
        assert abs(r.rank_ic) >= USEFUL_ABS_IC
        assert r.useful is False

    def test_first_run_top_factor_is_usable(self):
        """ret60 T+10 实测 IC -0.0697 / IR -0.37，必须判为反向可用。"""
        r = FactorICResult("ret60", 10, 225, -0.0697, 0.188, 37.0, 0.06, 3916.0)
        assert r.useful is True
        assert r.sign == -1
        assert r.verdict == "反向·可用"


class TestCompositeWeights:
    def test_weights_carry_sign_and_normalize(self):
        rs = [_res(0.05, 0.10, 65.0), FactorICResult("g", 5, 200, -0.06, 0.10, 35.0, 0.1, 3900.0)]
        rs[0] = FactorICResult("f", 5, 200, 0.05, 0.10, 65.0, 0.1, 3900.0)
        w = composite_weights(rs)
        assert set(w) == {"f", "g"}
        assert w["f"] > 0 and w["g"] < 0
        assert sum(abs(v) for v in w.values()) == pytest.approx(1.0, abs=1e-6)

    def test_excludes_unusable(self):
        rs = [
            FactorICResult("good", 5, 200, -0.06, 0.10, 35.0, 0.1, 3900.0),
            FactorICResult("noise", 5, 200, 0.004, 0.10, 50.0, 0.1, 3900.0),
        ]
        assert set(composite_weights(rs)) == {"good"}

    def test_empty_when_none_usable(self):
        assert composite_weights([_res(0.001, 0.10, 50.0)]) == {}


class TestRetention:
    def test_factor_ic_has_cleanup_rule(self):
        """必须挂进 db_maintenance 的清理规则，否则表会无限增长。"""
        from core.constants import TABLE_FACTOR_IC_DAILY
        from workflows.db_maintenance import CLEANUP_RULES

        hit = [r for r in CLEANUP_RULES if r[0] == TABLE_FACTOR_IC_DAILY]
        assert len(hit) == 1
        table, date_col, ttl, kind = hit[0]
        assert date_col == "eval_date"
        assert kind == "iso_date"
        # 该表价值在长期趋势，留存不应过短。
        assert ttl >= 180


class TestSchema:
    """schema 以 Python 常量版本化（本项目不留 .sql），故需测试防漂移。"""

    def test_ddl_is_idempotent(self):
        from core.factor_ic_schema import build_ddl

        ddl = build_ddl()
        assert "create table if not exists" in ddl
        assert ddl.count("create index if not exists") == 2

    def test_unique_key_matches_upsert_conflict_target(self):
        """DDL 的唯一约束必须与 save_factor_ic_rows 的 on_conflict 一致，否则重跑会累积。"""
        from pathlib import Path

        from core.factor_ic_schema import UNIQUE_KEY

        source = Path("integrations/supabase_factor_ic.py").read_text(encoding="utf-8")
        assert f'on_conflict="{",".join(UNIQUE_KEY)}"' in source

    def test_payload_keys_match_columns(self):
        """写入的字段集必须等于建表字段集（除 created_at 由 default 填）。"""
        import re
        from pathlib import Path

        from core.factor_ic_schema import payload_keys

        source = Path("integrations/supabase_factor_ic.py").read_text(encoding="utf-8")
        # 取 payload 列表推导整段：不能按第一个 "]" 截断，因为体内有 row["name"] 之类下标。
        block = source.split("payload = [")[1].split("for row in rows")[0]
        written = set(re.findall(r'^\s+"(\w+)":', block, re.M))
        assert written == set(payload_keys()), sorted(written ^ set(payload_keys()))

    def test_no_sql_files_in_repo(self):
        """本项目不允许 .sql 文件——quality_gate 也会拦，这里双保险。"""
        import subprocess

        out = subprocess.run(["git", "ls-files", "*.sql"], capture_output=True, text=True, check=False)
        assert out.stdout.strip() == "", f"发现 .sql 文件: {out.stdout}"
