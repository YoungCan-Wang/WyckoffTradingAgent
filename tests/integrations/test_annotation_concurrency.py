"""并发写不同图表不该互相覆盖。

_write 用 os.replace，保证的是「单次写」原子 —— 进程被杀不会留下截断的 JSON。
但 replace()/clear() 是三步：读整个 store → 改一个键 → 写回。中间没有任何东西
阻止另一个线程插进来，于是两个 IPC 线程同时改**不同图表**时，后写的那份基于
自己读到的旧快照，会把先写的那张图的标注整个抹掉。

IPC 侧是 ThreadPoolExecutor(max_workers=4)，所以这是同进程内的线程竞争。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from integrations import chart_annotations as ann


def _rect(low: float) -> dict[str, object]:
    return {
        "type": "rectangle",
        "start_date": "2026-08-01",
        "end_date": "2026-08-10",
        "low": low,
        "high": low + 5,
    }


def test_concurrent_writes_to_different_charts_all_survive(tmp_path):
    """20 张图并发写，每一张都必须留在最终的 store 里。"""
    store = tmp_path / "annotations.json"
    charts = [f"SYM{i:02d}:1d" for i in range(20)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda cid: ann.replace(cid, [_rect(10.0)], path=store), charts))

    final = ann.load_all(store)
    missing = [cid for cid in charts if cid not in final]
    assert missing == [], f"这些图的标注被其他线程的写覆盖掉了: {missing}"


def test_lock_actually_serializes_read_modify_write(tmp_path, monkeypatch):
    """直接验「读到写」这段区间不重叠，而不是只看最终结果。

    只断言最终结果的话，一次幸运的调度也能让测试变绿。这里在 load_all 和 _write
    之间插桩，人为放大窗口 —— 没有锁的话必然重叠。
    """
    store = tmp_path / "annotations.json"
    inside = 0
    overlapped = False
    guard = threading.Lock()

    real_load_all = ann.load_all
    real_write = ann._write

    def slow_load_all(path=None):
        nonlocal inside, overlapped
        with guard:
            inside += 1
            if inside > 1:
                overlapped = True
        # 给别的线程充分的机会挤进来
        threading.Event().wait(0.01)
        return real_load_all(path)

    def counting_write(target, data):
        nonlocal inside
        real_write(target, data)
        with guard:
            inside -= 1

    monkeypatch.setattr(ann, "load_all", slow_load_all)
    monkeypatch.setattr(ann, "_write", counting_write)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: ann.replace(f"S{i}:1d", [_rect(float(i))], path=store), range(8)))

    assert not overlapped, "两个线程同时处在读-改-写区间里 —— 锁没起作用"


def test_clear_is_also_serialized(tmp_path):
    """clear() 和 replace() 同形，别只给 replace 加锁。"""
    store = tmp_path / "annotations.json"
    charts = [f"C{i}:1d" for i in range(12)]
    for cid in charts:
        ann.replace(cid, [_rect(10.0)], path=store)

    # 一半清一半重写，交错跑
    def work(index: int):
        cid = charts[index]
        if index % 2 == 0:
            return ann.clear(cid, path=store)
        return ann.replace(cid, [_rect(20.0)], path=store)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(work, range(len(charts))))

    final = ann.load_all(store)
    for index, cid in enumerate(charts):
        if index % 2 == 0:
            assert cid not in final, f"{cid} 应已被清除"
        else:
            assert final.get(cid), f"{cid} 的重写丢了"


def test_validation_errors_do_not_hold_the_lock(tmp_path):
    """一批不合法的标注被拒之后，后续写入仍然正常。

    如果校验放在锁内且抛异常时没释放，第一条坏数据会把存储永久锁死。
    （用 with 语句本来就不会，但这条锁住这个性质。）
    """
    store = tmp_path / "annotations.json"
    for _ in range(3):
        try:
            ann.replace("BAD:1d", [{"type": "nope"}], path=store)
        except ann.AnnotationError:
            pass

    ann.replace("GOOD:1d", [_rect(10.0)], path=store)
    assert ann.load("GOOD:1d", path=store), "坏数据之后写不进去了 —— 锁没释放"
