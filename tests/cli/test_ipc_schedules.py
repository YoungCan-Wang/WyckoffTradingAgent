"""定时任务的增删改与启用开关。

桌面端原来只能看和手动跑一次 —— 这些方法把写能力接上去。重点在两件事：
坏 cron 绝不能落盘（会打死整个调度轮次），并发写不能互相覆盖。
"""

from __future__ import annotations

import logging
import threading

import pytest

from cli import scheduler
from cli.ipc.methods import MethodError, dispatch


@pytest.fixture(autouse=True)
def quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """每个测试用自己的 schedules.json，别碰用户真实文件。"""
    monkeypatch.setattr(scheduler, "SCHEDULES_PATH", tmp_path / "schedules.json")
    monkeypatch.setattr(scheduler, "LOCK_PATH", tmp_path / "schedules.lock")


def call(method: str, **params):
    events = list(dispatch(method, params))
    return next(e for e in events if e.get("type") == "result")


def make(name="盘前检查", cron="25 9 * * 1-5", action="/checkup", **extra) -> str:
    return call("schedule_create", name=name, cron=cron, action=action, **extra)["created"]["id"]


class TestList:
    def test_starts_empty_with_presets_offered(self):
        """一个任务都没有时要给推荐，否则页面是死的。"""
        result = call("schedules")
        assert result["schedules"] == []
        assert [p["id"] for p in result["presets"]] == ["mkt-open", "eod-review"]

    def test_added_preset_drops_out_of_suggestions(self):
        """列一个点了会重复的推荐等于埋坑。"""
        scheduler.save_schedules(
            [scheduler.Schedule(id="mkt-open", name="盘前风控检查", cron="25 9 * * 1-5", action="/checkup")]
        )
        assert [p["id"] for p in call("schedules")["presets"]] == ["eod-review"]

    def test_unreadable_file_is_an_error_not_an_empty_list(self):
        """坏文件返回空列表的话，用户新建一个就把原来的覆盖掉了。"""
        scheduler.SCHEDULES_PATH.write_text("{ 这不是 JSON", encoding="utf-8")
        with pytest.raises(MethodError) as exc:
            call("schedules")
        assert exc.value.code == "schedules_unreadable"


class TestCreate:
    def test_creates_enabled_with_next_run(self):
        created = call("schedule_create", name="盘前检查", cron="25 9 * * 1-5", action="/checkup")["created"]
        # 填完表单点确定，意图就是「让它开始跑」。还要再点一次开关只会让人以为没保存。
        assert created["enabled"] is True
        assert created["next_run"]

    def test_monthly_schedule_has_a_next_run(self):
        """search_days 是 8 时这里会是空串，卡片上显示「下次运行 —」。"""
        created = call("schedule_create", name="月末", cron="0 9 28 * *", action="x")["created"]
        assert created["next_run"]

    @pytest.mark.parametrize(
        "kwargs,code",
        [
            ({"name": "  ", "cron": "0 9 * * *", "action": "x"}, "invalid_params"),
            ({"name": "x", "cron": "0 9 * * *", "action": ""}, "invalid_params"),
            ({"name": "x", "cron": "1-5/2 9 * * *", "action": "y"}, "invalid_cron"),
            ({"name": "x", "cron": "60 9 * * *", "action": "y"}, "invalid_cron"),
            ({"name": "x", "cron": "bogus", "action": "y"}, "invalid_cron"),
        ],
    )
    def test_rejects_bad_input(self, kwargs, code):
        with pytest.raises(MethodError) as exc:
            call("schedule_create", **kwargs)
        assert exc.value.code == code

    def test_rejected_input_writes_nothing(self):
        with pytest.raises(MethodError):
            call("schedule_create", name="x", cron="bogus", action="y")
        assert call("schedules")["schedules"] == []

    def test_caps_the_count(self):
        scheduler.save_schedules(
            [scheduler.Schedule(id=f"s{i}", name=f"n{i}", cron="0 9 * * *", action="x") for i in range(40)]
        )
        with pytest.raises(MethodError) as exc:
            make()
        assert exc.value.code == "too_many"


class TestUpdate:
    def test_changes_only_what_was_passed(self):
        sid = make(name="原名", cron="25 9 * * 1-5", action="原内容")
        call("schedule_update", id=sid, cron="0 16 * * 5")
        stored = scheduler.load_schedules()[0]
        assert stored.cron == "0 16 * * 5"
        assert stored.name == "原名"
        assert stored.action == "原内容"

    def test_requires_at_least_one_field(self):
        """只传 id 说明前端有 bug，静默成功会掩盖它。"""
        with pytest.raises(MethodError) as exc:
            call("schedule_update", id=make())
        assert exc.value.code == "invalid_params"

    def test_unknown_id(self):
        with pytest.raises(MethodError) as exc:
            call("schedule_update", id="nope", cron="0 9 * * *")
        assert exc.value.code == "not_found"

    def test_bad_cron_leaves_the_old_one(self):
        sid = make(cron="25 9 * * 1-5")
        with pytest.raises(MethodError):
            call("schedule_update", id=sid, cron="99 9 * * *")
        assert scheduler.load_schedules()[0].cron == "25 9 * * 1-5"


class TestToggle:
    def test_round_trips(self):
        sid = make()
        assert call("schedule_toggle", id=sid, enabled=False)["updated"]["enabled"] is False
        assert call("schedule_toggle", id=sid, enabled=True)["updated"]["enabled"] is True

    def test_disabled_has_no_next_run(self):
        sid = make()
        assert call("schedule_toggle", id=sid, enabled=False)["updated"]["next_run"] == ""

    def test_enabling_validates_legacy_cron(self):
        """老任务可能是 TUI 或手改文件写进来的，从没被校验过。

        带着坏 cron 开启会打死整个调度轮次，所以开启前必须再看一眼。
        """
        scheduler.save_schedules(
            [scheduler.Schedule(id="old", name="老任务", cron="1-5/2 9 * * *", action="x", enabled=False)]
        )
        with pytest.raises(MethodError) as exc:
            call("schedule_toggle", id="old", enabled=True)
        assert exc.value.code == "invalid_cron"
        assert scheduler.load_schedules()[0].enabled is False

    def test_disabling_a_broken_one_still_works(self):
        """关闭是让它变安全，不该被它自己的坏值挡住。"""
        scheduler.save_schedules(
            [scheduler.Schedule(id="old", name="老", cron="1-5/2 9 * * *", action="x", enabled=True)]
        )
        assert call("schedule_toggle", id="old", enabled=False)["updated"]["enabled"] is False

    def test_requires_explicit_enabled(self):
        with pytest.raises(MethodError) as exc:
            call("schedule_toggle", id=make())
        assert exc.value.code == "invalid_params"


class TestDelete:
    def test_removes_only_the_target(self):
        keep, drop = make(name="留"), make(name="删")
        call("schedule_delete", id=drop)
        assert [s.id for s in scheduler.load_schedules()] == [keep]

    def test_deleting_twice_reports_not_found(self):
        sid = make()
        call("schedule_delete", id=sid)
        with pytest.raises(MethodError) as exc:
            call("schedule_delete", id=sid)
        assert exc.value.code == "not_found"

    def test_deleting_everything_is_a_stable_state(self):
        """删空之后不该重新长出预置任务。

        load_schedules 以前会在文件缺失时写回 DEFAULT_PRESETS，于是「一个都没有」
        不是稳定状态 —— 用户删完，下次打开又是两个。
        """
        call("schedule_delete", id=make())
        assert call("schedules")["schedules"] == []
        assert call("schedules")["schedules"] == []


class TestConcurrency:
    """save_schedules 是整文件覆写，读-改-写三步之间必须串起来。

    daemon 是独立进程且每 tick 写回 last_fired，所以用的是文件锁而非线程锁。
    """

    def test_parallel_creates_all_survive(self):
        errors: list[str] = []

        def create(i: int) -> None:
            try:
                make(name=f"任务{i}")
            except Exception as exc:  # noqa: BLE001 - 锁竞争不该抛
                errors.append(repr(exc))

        threads = [threading.Thread(target=create, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        stored = scheduler.load_schedules()
        assert len(stored) == 16, "有写入被覆盖了"
        assert len({s.id for s in stored}) == 16, "id 撞了"

    def test_parallel_toggles_do_not_lose_each_other(self):
        ids = [make(name=f"任务{i}") for i in range(8)]

        def turn_off(sid: str) -> None:
            call("schedule_toggle", id=sid, enabled=False)

        threads = [threading.Thread(target=turn_off, args=(sid,)) for sid in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert all(not s.enabled for s in scheduler.load_schedules())
