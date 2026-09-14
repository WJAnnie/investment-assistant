import time


class MarketCache:
    def __init__(self, clock=None):
        self._cache = {}
        self._clock = clock or time.time

    def set(self, key, value):
        self._cache[key] = {
            "value": value,
            "time": self._clock(),
        }

    def get(self, key, expire=300):
        if expire < 0:
            raise ValueError("expire must be non-negative")
        item = self._cache.get(key)
        if not item:
            return None
        if self._clock() - item["time"] >= expire:
            self._cache.pop(key, None)
            return None
        return item["value"]

    def clear(self):
        self._cache.clear()
