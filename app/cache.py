"""A small TTL cache.

This is a correctness feature, not a speed one. LinkedIn's rate budget is the
scarcest resource in the whole system — measured at roughly five guest requests
per IP before a multi-hour block — so serving a repeat lookup from memory is
what keeps the service alive under any real traffic.

In-process and therefore per-replica: two instances behind a load balancer keep
separate caches and spend separate budget. For a single-instance deployment
that is fine; scaling out means moving this to Redis, and the interface here is
deliberately narrow enough to make that a drop-in change.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    stored_at: float

    @property
    def age_seconds(self) -> int:
        return int(time.time() - self.stored_at)


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int, max_entries: int):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: OrderedDict[str, CacheEntry[T]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> CacheEntry[T] | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if time.time() - entry.stored_at > self._ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return entry

    def set(self, key: str, value: T) -> None:
        with self._lock:
            self._data[key] = CacheEntry(value=value, stored_at=time.time())
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def invalidate(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
