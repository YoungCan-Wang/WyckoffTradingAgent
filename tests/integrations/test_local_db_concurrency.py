"""连接的线程安全。

这一块被复审逮到过两次，两次都是 **exit 139（段错误）**而不是断言失败 ——
所以普通测试看不见，必须专门压。

根因是历史上共享一条连接 + `check_same_thread=False`：78 个调用点在多个线程里
并发操作同一个 sqlite3 对象，谁都不持锁。症状随机：
`cannot commit - no transaction is active`、
`executescript returned NULL without setting an exception`，
最糟的是另一个线程 close 掉连接后，剩下的在已释放句柄上跑 → 段错误。

现在每线程一条连接（见 get_db）。这些测试钉住那个不变式。
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    import core.constants as cc
    import integrations.local_db as ldb

    monkeypatch.setattr(cc, "LOCAL_DB_PATH", tmp_path / "t.db")
    ldb.reset_connection()
    ldb.init_db()
    yield ldb
    ldb.reset_connection()


def test_each_thread_gets_its_own_connection(db) -> None:
    """共享一条是段错误的根源 —— 这条不变式必须钉死。"""
    seen: dict[int, int] = {}

    def grab() -> None:
        seen[threading.get_ident()] = id(db.get_db())

    threads = [threading.Thread(target=grab) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    grab()  # 主线程也算一个

    assert len(set(seen.values())) == len(seen), "有线程共用了同一个连接对象"


def test_same_thread_reuses_its_connection(db) -> None:
    """不能每次调用都新建 —— 那样连接数会随请求数涨。"""
    assert db.get_db() is db.get_db()


def test_writes_from_another_thread_are_visible(db) -> None:
    """独立连接之后，跨线程可见性仍要成立（WAL + autocommit）。

    这是换成 thread-local 的主要风险：如果写在别的连接的未提交事务里，
    主线程就读不到 —— 那会变成「后台同步跑完了但界面看不到数据」。
    """
    errors: list[Exception] = []

    def writer() -> None:
        try:
            db.save_chat_log("s1", "user", "后台线程写的", user_id="alice")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=writer)
    t.start()
    t.join()

    assert not errors, f"后台线程写失败: {errors}"
    rows = db.load_chat_logs(session_id="s1", user_id="alice")
    assert [r["content"] for r in rows] == ["后台线程写的"]


def test_reset_while_other_threads_are_busy_does_not_crash(db) -> None:
    """**这就是 CI 上 exit 139 的形状。**

    一边有线程在读写，一边反复 reset_connection()（测试换库就是这么干的）。
    旧实现会 close 掉别的线程正在用的连接 → 段错误。

    现在 reset 只推进「代号」，各线程自己换 —— 不碰别人的连接对象。
    """
    stop = threading.Event()
    errors: list[Exception] = []

    def worker() -> None:
        while not stop.is_set():
            try:
                db.get_db().execute("SELECT COUNT(*) FROM chat_log").fetchone()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        for _ in range(80):
            db.reset_connection()
            db.init_db()
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)

    assert not errors, f"并发读 + reset 出错: {errors[:3]}"


def test_concurrent_writes_do_not_corrupt_transactions(db) -> None:
    """多线程同时写。共享连接时会撞出
    `cannot commit - no transaction is active` —— 那是两个线程的事务边界互踩。
    """
    errors: list[Exception] = []

    def writer(tag: str) -> None:
        try:
            for i in range(25):
                db.save_chat_log(f"{tag}-{i}", "user", "x", user_id="u")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"t{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写出错: {errors[:3]}"
    assert len(db.load_chat_logs(limit=500, user_id="u")) == 100
