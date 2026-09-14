"""市场闸门冻结：钉住生产禁买集，并把「什么条件才解锁」写成可读的判据。

为什么另开一个文件
------------------
``test_risk_on_reblocked.py`` 钉的是**逐档结论**（RISK_ON 禁、BEAR_REBOUND 放），
``tests/core/test_market_trade_mode.py`` 钉的是**两侧不错位**。两者都自己 setenv，
所以谁把 ``.env.example`` 里的 NEUTRAL 删掉，测试**全绿**——生产配置模板与测试之间
没有任何一条线。这个文件补的就是那条线：模板一动就红。

解锁条件（两条腿，缺一条都不解锁）
----------------------------------
NEUTRAL 现在被禁，档案上的理由不是「选股没用」。``core.market_trade_mode.
_explicitly_allowed_regimes`` 的 docstring 记着 formal_l4 口径的实测：配对超额
H=5 +6.99pct(t=+2.41)、H=10 +8.07pct(t=+2.47)，**超额是正的**；禁买是因为绝对收益
为负（-2.43% / -6.19%），那段窗口动量本身在亏钱，对照组只是亏得更多。

所以解锁要过的是两件独立的事：

1. **排序键方向**：影子车道反向臂判 ``排序键方向正确``。
   位置：``review_shadow_summary.json`` → ``score_direction.lanes.pre_breakout``
   → ``horizons.5.verdict``（``workflows/review_shadow_control.py``）。
   为什么是必要条件：解锁等于把 topN 交给 OMS 去买。符号反了的话，解锁的动作正好是
   「买最差那半」——闸门是当下唯一挡住这件事的东西。
   ``排序键符号反了`` / ``排序键无信息`` / ``方向未定`` / ``样本不足`` **都不算过**。
   尤其「样本不足」必须读成没有判定，不能读成「暂时放行」（证据规则 6：证据自相矛盾
   或缺失时取「不改」）。
2. **绝对收益**：上面那条只说「排得对」，没说「买了赚钱」。档案上的禁买理由是绝对
   收益为负，要么它在新窗口翻正，要么闸门改成按水温 beta 决定买不买，而不是靠这一档
   的名字。单靠第 1 条过线就解锁，等于用「排序对」回答了「该不该买」。

只认 ``pre_breakout``：全仓只有它带连续排序键。``rotation_setup`` 只作标签、老 trace
缺 ``watch_score``，反向臂对它们恒返回 ``ranked=False``（不适用，攒样本也不会变）。
那种「没有判定」不是证据，别拿它凑第 1 条。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.workflows.test_risk_on_reblocked import PROD_ALLOW, PROD_BLOCK

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

#: 生产禁买集。改这一行必须同时给出上面两条腿的证据，否则就是在无判定的情况下放行。
FROZEN_BLOCK = frozenset({"UNKNOWN", "NEUTRAL", "PANIC_REPAIR", "RISK_OFF", "CRASH", "BLACK_SWAN"})
#: 只有 BEAR_REBOUND 有豁免依据（.env.example 记的 +4.08pct，4/4 天为正）。
FROZEN_ALLOW = frozenset({"BEAR_REBOUND"})


def _env_example_value(key: str) -> str:
    """读模板里最后一次赋值。取最后一次是因为同名键重复时生效的是后者。"""
    found: str | None = None
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            found = stripped.split("=", 1)[1].strip()
    if found is None:
        raise AssertionError(f".env.example 缺少 {key}，闸门配置没了模板就没有可对照的生产值")
    return found


def _parse(raw: str) -> frozenset[str]:
    return frozenset(item.strip().upper() for item in raw.split(",") if item.strip())


class TestEnvTemplatePinned:
    """模板是唯一写下生产值的地方,它一动就得有人重新读一遍解锁条件。"""

    def test_block_list_matches_frozen_set(self) -> None:
        assert _parse(_env_example_value("STEP4_BUY_BLOCK_REGIMES")) == FROZEN_BLOCK

    def test_allow_list_matches_frozen_set(self) -> None:
        assert _parse(_env_example_value("STEP4_BUY_ALLOW_REGIMES")) == FROZEN_ALLOW

    def test_probe_downgrade_stays_empty(self) -> None:
        """留空 = 行为与改动前逐位相同。填 RISK_OFF 就是放开一只小仓,属于解锁动作。"""
        assert _env_example_value("STEP4_BUY_PROBE_REGIMES") == ""

    def test_sibling_pin_agrees_with_template(self) -> None:
        """与 test_risk_on_reblocked 的 PROD_* 交叉核对 —— 两处钉子不许各走各的。"""
        assert _parse(PROD_BLOCK) == FROZEN_BLOCK
        assert _parse(PROD_ALLOW) == FROZEN_ALLOW


class TestBothGatesSeeTheSameSet:
    """写入闸门与下单闸门错位过一次(2026-08,16 行买不到的正式推荐)。用生产值再钉一遍。"""

    @pytest.fixture(autouse=True)
    def _prod_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", PROD_BLOCK)
        monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", PROD_ALLOW)
        monkeypatch.setenv("STEP4_BUY_PROBE_REGIMES", "")

    def test_write_gate_and_order_gate_agree(self) -> None:
        from core.market_trade_mode import oms_buy_block_regimes
        from workflows.step4_order_config import step4_order_config_from_env

        assert set(oms_buy_block_regimes()) == set(step4_order_config_from_env().buy_block_regimes)

    def test_frozen_set_is_actually_blocked(self) -> None:
        """模板值经过 ALLOW/PROBE 相减后仍全数拦住 —— 光钉模板不够,还要钉解析结果。"""
        from core.market_trade_mode import oms_buy_block_regimes

        assert FROZEN_BLOCK <= set(oms_buy_block_regimes())

    def test_unlocking_requires_editing_this_file(self) -> None:
        """把 NEUTRAL 从 BLOCK 拿掉确实会放行 —— 证明这几条钉子钉在真的开关上。

        机制在,所以「解锁」是一次显式的配置改动,不是某天自己漂走的。
        """
        from core.market_trade_mode import oms_buy_block_regimes
        from workflows.step4_order_config import step4_order_config_from_env

        loosened = ",".join(sorted(FROZEN_BLOCK - {"NEUTRAL"}))
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("STEP4_BUY_BLOCK_REGIMES", loosened)
            assert "NEUTRAL" not in oms_buy_block_regimes()
            assert "NEUTRAL" not in step4_order_config_from_env().buy_block_regimes
