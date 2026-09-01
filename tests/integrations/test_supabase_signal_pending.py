from __future__ import annotations

import pytest

from integrations import supabase_signal_pending as mod


class _UniqueConflict(Exception):
    """模拟 PostgREST 报回来的 23505，文本照抄 2026-08-31 生产日志。"""

    def __init__(self, code: int, signal_type: str) -> None:
        super().__init__(
            "{'message': 'duplicate key value violates unique constraint "
            f"\"uq_signal_pending_active\"', 'code': '23505', 'details': 'Key (code, signal_type)=({code}, "
            f"{signal_type}) already exists.'}}"
        )


class _Query:
    """按 uq_signal_pending_active 的语义建模：活跃态的 (code, signal_type) 占用唯一键。"""

    def __init__(self, existing: list[dict], *, enforce_unique: bool = True) -> None:
        self.existing = existing
        self.inserted: list[dict] = []
        self.insert_calls = 0
        self.enforce_unique = enforce_unique
        # 快照里看不见、insert 时才冲突的键，用来模拟读后写竞态。
        self.conflict_keys: set[tuple[int, str]] = set()
        self._status_filter: list[str] | None = None
        self._date_filter: list[str] | None = None

    def select(self, *_args):
        self._status_filter = None
        self._date_filter = None
        return self

    def eq(self, *_args):
        return self

    def in_(self, column, values):
        if column == "status":
            self._status_filter = list(values)
        elif column == "signal_date":
            self._date_filter = [str(v) for v in values]
        return self

    def execute(self):
        rows = self.existing
        if self._status_filter is not None:
            rows = [r for r in rows if r.get("status", "pending") in self._status_filter]
        if self._date_filter is not None:
            rows = [r for r in rows if str(r.get("signal_date") or "") in self._date_filter]
        return type("Result", (), {"data": rows})()

    def _active_keys(self) -> set[tuple[int, str]]:
        return {
            (int(r["code"]), r["signal_type"])
            for r in self.existing
            if r.get("status", "pending") in mod.ACTIVE_SIGNAL_STATUSES
        }

    def insert(self, rows):
        self.insert_calls += 1
        if self.enforce_unique:
            taken = self._active_keys() | self.conflict_keys
            for row in rows:
                key = (int(row["code"]), row["signal_type"])
                if key in taken:
                    raise _UniqueConflict(*key)
                taken.add(key)
        self.inserted.extend(rows)
        self.existing = [*self.existing, *rows]
        return self


class _Client:
    def __init__(self, existing: list[dict], *, enforce_unique: bool = True) -> None:
        self.query = _Query(existing, enforce_unique=enforce_unique)

    def table(self, _name):
        return self.query


@pytest.fixture
def _writable(monkeypatch):
    monkeypatch.setattr(mod, "_configured", lambda: True)
    monkeypatch.setattr(mod, "require_server_write_context", lambda *_args: None)

    def _bind(client):
        monkeypatch.setattr(mod, "_admin", lambda: client)
        return client

    return _bind


def test_new_date_blocked_while_prior_signal_still_active(_writable) -> None:
    """跨日但旧行仍活跃 → 不能写。

    这是 2026-08-31 的真实形状：912/sos 在 8-28 是 survived，8-31 又触发。
    唯一约束只看 (code, signal_type) + 活跃态，不看 signal_date，所以必须在写之前挡住。
    """
    client = _writable(
        _Client([{"code": 600611, "signal_type": "sos", "signal_date": "2026-07-15", "status": "survived"}])
    )
    rows = [{"code": 600611, "signal_type": "sos", "signal_date": "2026-07-16"}]

    assert mod.insert_pending_signal_rows(rows) == 0
    assert client.query.inserted == []


def test_new_date_allowed_once_prior_signal_settled(_writable) -> None:
    """旧行已 expired/confirmed → 不再占用唯一键，同一票可以重新挂 pending。"""
    client = _writable(
        _Client([{"code": 600611, "signal_type": "sos", "signal_date": "2026-07-15", "status": "expired"}])
    )
    rows = [{"code": 600611, "signal_type": "sos", "signal_date": "2026-07-16"}]

    assert mod.insert_pending_signal_rows(rows) == 1
    assert client.query.inserted == rows


def test_pending_dedup_skips_same_trade_date(_writable) -> None:
    existing = [{"code": 600611, "signal_type": "sos", "signal_date": "2026-07-16", "status": "pending"}]
    client = _writable(_Client(existing))

    assert mod.insert_pending_signal_rows([dict(existing[0])]) == 0
    assert client.query.inserted == []


def test_pending_dedup_skips_same_date_after_confirmation(_writable) -> None:
    """已 confirmed 的当天信号不该被重复挂回 pending。

    这一条不由唯一约束保证（confirmed 不占活跃键），靠当天那道过滤挡住 —— 同一天
    重跑漏斗时不能把已判定的信号重新挂成 pending。
    """
    client = _writable(
        _Client([{"code": 600611, "signal_type": "sos", "signal_date": "2026-07-16", "status": "confirmed"}])
    )
    pending = [{"code": 600611, "signal_type": "sos", "signal_date": "2026-07-16"}]

    assert mod.insert_pending_signal_rows(pending) == 0
    assert client.query.inserted == []


def test_duplicate_keys_within_one_batch_collapse(_writable) -> None:
    """同一批里重复的键要先自去重，否则批量 insert 会自己跟自己撞。"""
    client = _writable(_Client([]))
    rows = [
        {"code": 600611, "signal_type": "sos", "signal_date": "2026-07-16", "signal_score": 1.0},
        {"code": 600611, "signal_type": "sos", "signal_date": "2026-07-16", "signal_score": 2.0},
    ]

    assert mod.insert_pending_signal_rows(rows) == 1
    assert client.query.inserted == [rows[0]]


def test_one_conflict_does_not_lose_the_whole_batch(_writable) -> None:
    """核心回归：残留竞态只该吃掉冲突那一行。

    2026-08-31 是 140 条触发信号被一行 912/sos 的冲突整批回滚（signal_pending 里 8-31
    零行，8-28 有 38 行、8-27 有 60 行）。这里让过滤读到的快照是空的、insert 时才冒出
    冲突，模拟读后写竞态：批量失败后应逐行重试，只丢冲突那一行。
    """
    client = _writable(_Client([]))
    # 快照为空（过滤读不到），但 insert 时 912/sos 已被占用 —— 读后写的竞态窗口。
    client.query.conflict_keys = {(912, "sos")}
    rows = [
        {"code": 100, "signal_type": "sos", "signal_date": "2026-08-31"},
        {"code": 912, "signal_type": "sos", "signal_date": "2026-08-31"},
        {"code": 300, "signal_type": "lps", "signal_date": "2026-08-31"},
    ]

    assert mod.insert_pending_signal_rows(rows) == 2
    assert [r["code"] for r in client.query.inserted] == [100, 300]
    # 一次批量（失败）+ 三次逐行，而不是整批丢掉。
    assert client.query.insert_calls == 4


def test_schema_miss_still_falls_back_to_legacy_payload(_writable) -> None:
    """老的缺列降级不能被新的冲突分支挤掉。"""
    client = _writable(_Client([], enforce_unique=False))
    calls: list[list[dict]] = []

    def _insert(rows):
        calls.append(rows)
        if len(calls) == 1:
            raise RuntimeError("Could not find the 'candidate_theme' column of 'signal_pending' in the schema cache")
        client.query.inserted.extend(rows)
        return client.query

    client.query.insert = _insert  # type: ignore[method-assign]
    rows = [{"code": 600611, "signal_type": "sos", "signal_date": "2026-07-16", "candidate_theme": "AI"}]

    assert mod.insert_pending_signal_rows(rows) == 1
    assert "candidate_theme" not in client.query.inserted[0]
