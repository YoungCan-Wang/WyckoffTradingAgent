from __future__ import annotations

from datetime import datetime

import pytest

from cli import scheduler
from cli.scheduler import Schedule, cron_matches_now, next_scheduled_time, schedule_status


def test_cron_matches_now_uses_provided_time_instead_of_wall_clock():
    at = datetime(2026, 1, 5, 9, 25)  # Monday

    assert cron_matches_now("25 9 * * 1-5", at=at)
    assert not cron_matches_now("26 9 * * 1-5", at=at)


def test_cron_matches_now_checks_weekday_field():
    saturday = datetime(2026, 1, 3, 9, 25)

    assert not cron_matches_now("25 9 * * 1-5", at=saturday)


def test_schedule_status_includes_next_run_and_last_result():
    at = datetime(2026, 7, 13, 9, 24)
    schedule = Schedule(
        id="mkt-open",
        name="盘前风控检查",
        cron="25 9 * * 1-5",
        action="/checkup",
        last_fired="2026-07-10T09:25",
        last_status="triggered",
    )

    assert next_scheduled_time(schedule, at=at) == datetime(2026, 7, 13, 9, 25)
    assert schedule_status([schedule], at=at) == [
        {
            "id": "mkt-open",
            "name": "盘前风控检查",
            "enabled": True,
            "cron": "25 9 * * 1-5",
            # 编辑表单要回填 action，所以它在投影里
            "action": "/checkup",
            "last_fired": "2026-07-10T09:25",
            "last_status": "triggered",
            "last_error": "",
            "next_run": "2026-07-13T09:25",
        }
    ]


class TestValidateCron:
    """落盘前的校验。

    一个坏 cron 不只是它自己不触发：`_field_matches` 里的 int() 抛 ValueError，
    穿出 cron_matches_now，被 daemon 的宽 except 抓住 —— 那一轮**所有**任务都被
    跳过。所以坏值绝不能进文件。
    """

    @pytest.mark.parametrize(
        "cron",
        [
            "25 9 * * 1-5",  # 工作日
            "0 16 * * 5",  # 每周五
            "0 9 5 * *",  # 每月 5 号
            "*/15 * * * *",  # 每 15 分钟
            "0 0 1 1 *",  # 边界：全下限
            "59 23 31 12 6",  # 边界：全上限
        ],
    )
    def test_accepts_valid(self, cron):
        assert scheduler.validate_cron(cron) is None

    def test_rejects_range_with_step(self):
        """后端在这个形状上会崩，不是「不匹配」。"""
        assert "范围/步长" in scheduler.validate_cron("1-5/2 9 * * *")

    @pytest.mark.parametrize(
        "cron,reason",
        [
            ("", "不能为空"),
            ("bogus", "5 个字段"),
            ("1 2 3 4", "5 个字段"),
            ("1 2 3 4 5 6", "5 个字段"),
            ("abc 9 * * *", "分钟"),
            ("60 9 * * *", "分钟"),
            ("0 24 * * *", "小时"),
            ("0 9 32 * *", "日"),
            ("0 9 * 13 *", "月"),
            ("0 9 * * 7", "星期"),
            ("5-1 9 * * *", "反了"),
            ("0 9 * * 1,, 2".replace(" ", " "), "字段"),
        ],
    )
    def test_rejects_invalid(self, cron, reason):
        problem = scheduler.validate_cron(cron)
        assert problem is not None, f"{cron!r} 应该被拒"
        assert reason in problem

    def test_every_rejected_cron_is_safe_to_evaluate(self):
        """被拒的串里，那些会让 cron_matches_now 崩的必须真的被拦住。"""
        crashers = ["1-5/2 9 * * *", "abc 9 * * *", "0 9 * * x"]
        for cron in crashers:
            assert scheduler.validate_cron(cron) is not None
            with pytest.raises(Exception):
                scheduler.cron_matches_now(cron, datetime(2026, 8, 25, 9, 0))


class TestNextRunSearchWindow:
    """每月任务要算得出下次运行。

    窗口原来是 8 天，「每月 5 号」「每月 20 号」都返回 None —— 卡片上显示
    「下次运行 —」，看起来像功能坏了。
    """

    def _sched(self, cron):
        return scheduler.Schedule(id="t", name="t", cron=cron, action="x", enabled=True)

    @pytest.mark.parametrize("day", [1, 5, 15, 20, 28])
    def test_monthly_resolves(self, day):
        now = datetime(2026, 8, 25, 10, 0)
        assert scheduler.next_scheduled_time(self._sched(f"0 9 {day} * *"), at=now) is not None

    def test_month_end_skips_short_months(self):
        """每月 31 号：2 月没有那天就跳过，落到 3 月。"""
        found = scheduler.next_scheduled_time(
            self._sched("0 9 31 * *"), at=datetime(2026, 2, 1, 0, 0)
        )
        assert found == datetime(2026, 3, 31, 9, 0)

    def test_disabled_has_no_next_run(self):
        s = self._sched("0 9 * * *")
        s.enabled = False
        assert scheduler.next_scheduled_time(s, at=datetime(2026, 8, 25, 10, 0)) is None
