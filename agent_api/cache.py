"""Small bounded process-local TTL caches for non-security-sensitive data."""

import threading
import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, maxsize=2048, timer=time.monotonic):
        self.maxsize = maxsize
        self.timer = timer
        self._items = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None, False
            expires, value = item
            if expires <= self.timer():
                del self._items[key]
                return None, False
            self._items.move_to_end(key)
            return value, True

    def set(self, key, value, ttl):
        with self._lock:
            self._items[key] = (self.timer() + max(0, ttl), value)
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def clear(self):
        with self._lock:
            self._items.clear()


reference_cache = TTLCache(maxsize=5000)
dashboard_cache = TTLCache(maxsize=512)
