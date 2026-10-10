"""共用服务里「全市场结果」的短时缓存。

只缓存**不依赖任何用户凭据、结果对所有人一样**的工具（见 CACHE_TTL_SECONDS）。带用户 Key 的工具
（如 intraday_rescue_check）永远不进缓存：取数用的是各自的 Key，结果和额度都不该共享。

同一个请求同时到达时只算一次，其余等待并共享结果；出错的结果不缓存，下一次会重算。
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

# 这两个工具只读公开行情、不带用户凭据；行情分钟级变化，5 分钟的陈旧度对市况判断可以接受。
CACHE_TTL_SECONDS: dict[str, float] = {"market_regime": 300.0, "wyckoff_diagnose": 300.0}
MAX_ENTRIES = 256


def _is_error(value: Any) -> bool:
    return isinstance(value, dict) and "error" in value


class ToolResultCache:
    def __init__(
        self,
        ttls: dict[str, float] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self._ttls = dict(CACHE_TTL_SECONDS if ttls is None else ttls)
        self._clock = clock
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], tuple[float, Any]] = OrderedDict()
        self._flights: dict[tuple[str, str], threading.Lock] = {}

    def get_or_compute(self, name: str, args: dict[str, Any], compute: Callable[[], Any]) -> tuple[Any, bool]:
        """返回 (结果, 是否来自缓存)。compute 只在需要时才被调用，且同一个键同一时刻最多一个。"""
        ttl = self._ttls.get(name)
        if not ttl:
            return compute(), False
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str))
        with self._lock:
            hit = self._fresh(key)
            if hit is not None:
                return hit[0], True
            flight = self._flights.setdefault(key, threading.Lock())
        with flight:
            try:
                with self._lock:
                    hit = self._fresh(key)
                if hit is not None:
                    return hit[0], True
                value = compute()
                if not _is_error(value):
                    with self._lock:
                        self._store(key, value, ttl)
                return value, False
            finally:
                with self._lock:
                    self._flights.pop(key, None)

    def _fresh(self, key: tuple[str, str]) -> tuple[Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry[0] <= self._clock():
            del self._entries[key]
            return None
        return (entry[1],)

    def _store(self, key: tuple[str, str], value: Any, ttl: float) -> None:
        self._entries[key] = (self._clock() + ttl, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
