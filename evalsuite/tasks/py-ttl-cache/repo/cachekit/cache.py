"""A small least-recently-used cache."""

from collections import OrderedDict

_MISSING = object()


class LRUCache:
    def __init__(self, maxsize: int):
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self._data: OrderedDict = OrderedDict()

    def get(self, key, default=None):
        value = self._data.get(key, _MISSING)
        if value is _MISSING:
            return default
        self._data.move_to_end(key)
        return value

    def set(self, key, value) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def __contains__(self, key) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)
