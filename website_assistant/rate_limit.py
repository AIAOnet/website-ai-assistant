"""Bounded rolling-window limiter for public generation and protected probes."""
from collections import OrderedDict, deque
from threading import Lock
from time import monotonic


class RateLimiter:
    def __init__(self, limit, window=60, clock=monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.clients, self.lock = OrderedDict(), Lock()

    def allow(self, key):
        now = self.clock()
        with self.lock:
            times = self.clients.pop(key, deque())
            while times and times[0] <= now - self.window:
                times.popleft()
            allowed = len(times) < self.limit
            if allowed:
                times.append(now)
            self.clients[key] = times
            while len(self.clients) > 10000:
                self.clients.popitem(last=False)
            return allowed
