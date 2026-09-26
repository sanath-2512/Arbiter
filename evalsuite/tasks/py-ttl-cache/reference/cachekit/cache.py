"""A small least-recently-used cache."""

import time
from collections import OrderedDict

_MISSING = object()


class LRUCache:
    def __init__(self, maxsize: int, ttl=None, clock=time.monotonic):
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self.ttl = ttl
        self.clock = clock
        self._data: OrderedDict = OrderedDict()

    def _expired(self, key) -> bool:
        if self.ttl is None:
            return False
        _, stamp = self._data[key]
        return self.clock() - stamp >= self.ttl

    def _purge(self) -> None:
        for key in [k for k in self._data if self._expired(k)]:
            del self._data[key]

    def get(self, key, default=None):
        if key not in self._data or self._expired(key):
            self._data.pop(key, None)
            return default
        self._data.move_to_end(key)
        return self._data[key][0]

    def set(self, key, value) -> None:
        self._data[key] = (value, self.clock())
        self._data.move_to_end(key)
        self._purge()
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def __contains__(self, key) -> bool:
        return key in self._data and not self._expired(key)

    def __len__(self) -> int:
        self._purge()
        return len(self._data)
