"""清除止损不该和设置止损共享免审批档位。

set_stop_loss 原本整个工具都在 AUTO_TOOLS 里，理由写在注释上：「安全性来自工具
本身能做的事很窄 —— 不接受股数、成本、现金参数」。

这个前提在支持 stop_loss=None 清除止损之后就不成立了。「设置一道保护」和「移除
一道保护」的风险方向完全相反，而 classify() 当时不看参数，于是 daemon 可以在
无人值守时自动执行 agent 提出的「把止损撤掉」—— 一个纯粹增加风险敞口的动作，
走的却是为「纯粹降低风险」设计的免审批通道。
"""

from __future__ import annotations

import pytest

from cli import approval_policy as policy


def test_setting_a_stop_stays_auto():
    """设置止损仍然免审批 —— 这条通道存在的理由没有变。"""
    assert policy.classify("set_stop_loss", {"code": "600519", "stop_loss": 1400}) == policy.AUTO


@pytest.mark.parametrize("cleared", [None, "", "   "])
def test_clearing_a_stop_needs_review(cleared):
    """清除止损必须有人过一眼。空串下游也当没有值，一并算清除。"""
    args = {"code": "600519", "stop_loss": cleared}
    assert policy.classify("set_stop_loss", args) == policy.REVIEW


def test_missing_field_is_not_treated_as_clearing():
    """压根没传 stop_loss 是参数不全，交给下游报错，不该在这里升档。

    如果用 args.get("stop_loss") is None 判断，这个用例会和「传了 null」混为一谈。
    """
    assert policy.classify("set_stop_loss", {"code": "600519"}) == policy.AUTO


def test_reason_distinguishes_the_two():
    """界面上的理由也要分开。

    否则清除止损的审批卡会显示「该工具只能改止损价，无人时可自动执行」——
    一句与它出现在审批列表这件事自相矛盾的话。
    """
    assert policy.explain("set_stop_loss", {"code": "x", "stop_loss": 1400}) == "reason.auto_narrow_tool"
    assert policy.explain("set_stop_loss", {"code": "x", "stop_loss": None}) == "reason.clears_stop_loss"


def test_headless_does_not_auto_allow_clearing():
    """走真实的 headless 闸门确认一遍，而不是只测 classify。

    classify 对了但闸门读的是别的判定函数，等于没修 —— 曾经有个
    is_auto_tool(tool_name) 只看工具名，正是这种绕过。
    """
    from cli import headless

    # 按行为找闸门类，而不是写死类名 —— 改名不该让这条测试变绿或变红
    # （这里要的是「闸门确实拒绝」，不是「某个名字存在」）。
    gate_cls = next(
        (
            obj
            for obj in vars(headless).values()
            if isinstance(obj, type) and hasattr(obj, "confirm") and hasattr(obj, "set_nav")
        ),
        None,
    )
    assert gate_cls is not None, "找不到 headless 的审批闸门类"

    gate = gate_cls(source="test")
    allow_set = gate.confirm("set_stop_loss", {"code": "600519", "stop_loss": 1400})
    assert allow_set.get("action") == "allow", "设置止损应仍然直接放行"

    decision = gate.confirm("set_stop_loss", {"code": "600519", "stop_loss": None})
    assert decision.get("action") != "allow", "清除止损绝不能被无人自动放行"


def test_auto_tools_membership_alone_is_not_the_gate():
    """在 AUTO_TOOLS 里不等于放行 —— 参数检查才是。

    这条锁住设计意图：将来往 AUTO_TOOLS 加工具的人，必须同时想清楚
    「这个工具有没有反方向的参数组合」。
    """
    assert "set_stop_loss" in policy.AUTO_TOOLS
    assert policy.classify("set_stop_loss", {"stop_loss": None}) != policy.AUTO
