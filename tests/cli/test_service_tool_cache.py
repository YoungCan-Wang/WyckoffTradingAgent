"""全市场工具结果缓存：只缓存成功结果、同键同时只算一次、过期重算、容量有界。"""

from __future__ import annotations

import threading

from cli.service.tool_cache import ToolResultCache


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _counter(value=None):
    calls = []

    def compute():
        calls.append(1)
        return value if value is not None else {"n": len(calls)}

    return compute, calls


def test_second_call_is_served_from_the_cache():
    cache = ToolResultCache({"market_regime": 60})
    compute, calls = _counter()

    first = cache.get_or_compute("market_regime", {}, compute)
    second = cache.get_or_compute("market_regime", {}, compute)

    assert first == ({"n": 1}, False)
    assert second == ({"n": 1}, True)
    assert len(calls) == 1


def test_a_tool_without_a_ttl_is_never_cached():
    cache = ToolResultCache({"market_regime": 60})
    compute, calls = _counter()

    cache.get_or_compute("intraday_rescue_check", {"code": "600519"}, compute)
    cache.get_or_compute("intraday_rescue_check", {"code": "600519"}, compute)

    assert len(calls) == 2


def test_different_arguments_are_different_entries_and_key_order_does_not_matter():
    cache = ToolResultCache({"wyckoff_diagnose": 60})
    compute, calls = _counter()

    cache.get_or_compute("wyckoff_diagnose", {"code": "600519", "x": 1}, compute)
    cache.get_or_compute("wyckoff_diagnose", {"x": 1, "code": "600519"}, compute)
    cache.get_or_compute("wyckoff_diagnose", {"code": "000001", "x": 1}, compute)

    assert len(calls) == 2


def test_entries_expire():
    clock = _Clock()
    cache = ToolResultCache({"market_regime": 60}, clock=clock)
    compute, calls = _counter()

    cache.get_or_compute("market_regime", {}, compute)
    clock.now += 59
    assert cache.get_or_compute("market_regime", {}, compute)[1] is True
    clock.now += 2
    assert cache.get_or_compute("market_regime", {}, compute)[1] is False
    assert len(calls) == 2


def test_error_results_are_not_cached():
    cache = ToolResultCache({"market_regime": 60})
    results = iter([{"error": "上游超时"}, {"regime": "RISK_OFF"}])

    first = cache.get_or_compute("market_regime", {}, lambda: next(results))
    second = cache.get_or_compute("market_regime", {}, lambda: next(results))
    third = cache.get_or_compute("market_regime", {}, lambda: next(results))

    assert first == ({"error": "上游超时"}, False)
    assert second == ({"regime": "RISK_OFF"}, False)
    assert third == ({"regime": "RISK_OFF"}, True)


def test_an_exception_is_not_cached_and_does_not_wedge_the_key():
    cache = ToolResultCache({"market_regime": 60})

    def boom():
        raise RuntimeError("x")

    try:
        cache.get_or_compute("market_regime", {}, boom)
    except RuntimeError:
        pass

    assert cache.get_or_compute("market_regime", {}, lambda: {"ok": True}) == ({"ok": True}, False)


def test_concurrent_identical_requests_compute_once():
    cache = ToolResultCache({"market_regime": 60})
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []
    results: list[tuple] = []

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return {"regime": "RISK_OFF"}

    def worker():
        results.append(cache.get_or_compute("market_regime", {}, slow))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    threads[0].start()
    assert started.wait(5)
    for thread in threads[1:]:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(5)

    assert len(calls) == 1
    assert sorted(cached for _, cached in results) == [False, True, True, True]


def test_the_cache_is_bounded():
    cache = ToolResultCache({"wyckoff_diagnose": 60}, max_entries=2)
    compute, calls = _counter()

    for code in ("a", "b", "c"):
        cache.get_or_compute("wyckoff_diagnose", {"code": code}, compute)
    cache.get_or_compute("wyckoff_diagnose", {"code": "a"}, compute)  # 最旧的已被挤出，要重算

    assert len(calls) == 4


def test_the_default_cache_only_covers_credential_free_market_wide_tools():
    from cli.service.tool_cache import CACHE_TTL_SECONDS

    assert set(CACHE_TTL_SECONDS) == {"market_regime", "wyckoff_diagnose"}
