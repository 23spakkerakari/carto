"""Token-bucket rate limits per IP and per user (spec 14.7 "Rate limits: per user and per IP;
stricter on login").

The bucket holds ``rate_per_minute`` tokens and refills continuously at that rate; a request
takes one token or is refused with the seconds until the next one (``Retry-After``). Buckets
live in process memory, so the limit is per replica: one replica serves the M2 pilot (ADR
0052), and a shared limiter (the sessions database or a cache) is an M6 item together with the
other multi-replica concerns. Idle buckets are dropped so a scan of many addresses cannot grow
the table without bound (spec 2.3 invariant 8).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Final

__all__ = ["TokenBucketLimiter"]

_MAX_KEYS: Final = 100_000


class TokenBucketLimiter:
    def __init__(self, rate_per_minute: int, clock: Callable[[], datetime]) -> None:
        if rate_per_minute < 1:
            msg = "rate_per_minute must be at least 1"
            raise ValueError(msg)
        self._capacity = float(rate_per_minute)
        self._per_second = rate_per_minute / 60.0
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}
        """key -> (tokens, timestamp of that measurement)."""

    def check(self, key: str) -> int | None:
        """Take a token for ``key``; ``None`` when allowed, else whole seconds to wait."""
        now = self._clock().timestamp()
        with self._lock:
            tokens, at = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - at) * self._per_second)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                self._prune(now)
                return None
            self._buckets[key] = (tokens, now)
            self._prune(now)
            return max(1, math.ceil((1.0 - tokens) / self._per_second))

    def _prune(self, now: float) -> None:
        """Drop buckets that are full again (idle for a full refill) when the table is large."""
        if len(self._buckets) < _MAX_KEYS:
            return
        refill_seconds = self._capacity / self._per_second
        for key in [k for k, (_t, at) in self._buckets.items() if now - at >= refill_seconds]:
            del self._buckets[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)
