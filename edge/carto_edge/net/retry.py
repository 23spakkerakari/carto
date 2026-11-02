"""Retries with jittered exponential backoff, a circuit breaker and a rate limiter (spec 8.1).

Spec 8.1, common requirements for every connector: "Timeouts, retries with jittered exponential
backoff, circuit breaker per source. Rate limits per source (requests/s and concurrent
queries)." Everything here is clock- and sleep-injectable so tests run without waiting.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from types import TracebackType
from typing import Final, Self

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "RateLimiter",
    "backoff_delay",
    "backoff_delays",
    "retry_async",
]

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

DEFAULT_JITTER: Final = 0.5


def _check_backoff(base: float, cap: float, jitter: float) -> None:
    if base < 0:
        msg = "backoff base must be non-negative"
        raise ValueError(msg)
    if cap < base:
        msg = "backoff cap must be at least the base"
        raise ValueError(msg)
    if not 0.0 <= jitter <= 1.0:
        msg = "backoff jitter must be between 0 and 1"
        raise ValueError(msg)


def backoff_delay(
    attempt: int,
    *,
    base: float,
    cap: float,
    jitter: float = DEFAULT_JITTER,
    rng: Callable[[], float] = random.random,
) -> float:
    """Delay after failed attempt number ``attempt`` (1-based): ``min(cap, base * 2**(attempt-1))``
    scaled by a factor in ``[1 - jitter, 1 + jitter]`` and clamped to ``[0, cap]``."""
    if attempt < 1:
        msg = "backoff attempt numbers are 1-based"
        raise ValueError(msg)
    _check_backoff(base, cap, jitter)
    nominal = min(cap, base * (2.0 ** (attempt - 1)))
    factor = 1.0 + jitter * (2.0 * rng() - 1.0)
    return min(cap, max(0.0, nominal * factor))


def backoff_delays(
    attempts: int,
    *,
    base: float,
    cap: float,
    jitter: float = DEFAULT_JITTER,
    rng: Callable[[], float] = random.random,
) -> list[float]:
    """The delays after each of ``attempts`` failed attempts, in order."""
    if attempts < 1:
        msg = "backoff attempts must be at least 1"
        raise ValueError(msg)
    _check_backoff(base, cap, jitter)
    return [
        backoff_delay(attempt, base=base, cap=cap, jitter=jitter, rng=rng)
        for attempt in range(1, attempts + 1)
    ]


async def retry_async[T](
    op: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base: float,
    cap: float,
    retry_on: Sequence[type[BaseException]],
    jitter: float = DEFAULT_JITTER,
    sleep: Sleep = asyncio.sleep,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
) -> T:
    """Run ``op`` up to ``attempts`` times, sleeping a jittered exponential delay between tries.

    Only exceptions that are instances of a type in ``retry_on`` are retried; anything else
    propagates at once. The last exception propagates when every attempt failed. ``on_retry``
    sees ``(attempt, delay, exception)`` before each sleep, for logging counts and durations.
    """
    if attempts < 1:
        msg = "attempts must be at least 1"
        raise ValueError(msg)
    _check_backoff(base, cap, jitter)
    retry_types = tuple(retry_on)
    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except retry_types as exc:
            if attempt >= attempts:
                raise
            delay = backoff_delay(attempt, base=base, cap=cap, jitter=jitter)
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            await sleep(delay)
    msg = "unreachable: the loop returns or raises"  # pragma: no cover
    raise AssertionError(msg)  # pragma: no cover


# ---------------------------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------------------------


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """The breaker is open: the source is being left alone until the reset period passes."""


class CircuitBreaker:
    """Closed until ``failure_threshold`` consecutive failures; then open for ``reset_seconds``;
    then half-open, letting exactly one probe through: success closes, failure reopens."""

    __slots__ = ("_clock", "_failures", "_opened_at", "_probe_in_flight", "_reset", "_threshold")

    def __init__(
        self, failure_threshold: int, reset_seconds: float, clock: Clock = time.monotonic
    ) -> None:
        if failure_threshold < 1:
            msg = "failure_threshold must be at least 1"
            raise ValueError(msg)
        if reset_seconds <= 0:
            msg = "reset_seconds must be positive"
            raise ValueError(msg)
        self._threshold = failure_threshold
        self._reset = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def failures(self) -> int:
        return self._failures

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - self._opened_at >= self._reset:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def allow(self) -> bool:
        """Whether a call may proceed now. In the half-open state only one probe is allowed."""
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.OPEN:
            return False
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def check(self) -> None:
        if not self.allow():
            msg = "circuit breaker is open"
            raise CircuitOpenError(msg)

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        self._probe_in_flight = False
        if self._opened_at is not None or self._failures >= self._threshold:
            self._opened_at = self._clock()

    def release_probe(self) -> None:
        """Forget an unfinished probe without counting it (a cancelled call): the next caller
        in the half-open state may probe again."""
        self._probe_in_flight = False

    async def call[T](self, op: Callable[[], Awaitable[T]]) -> T:
        """Run ``op`` through the breaker: refused when open, recorded otherwise. A call ended
        by cancellation (or another ``BaseException``) is neither a success nor a failure, but
        it frees the half-open probe slot, so the breaker can never stay wedged half-open."""
        self.check()
        try:
            result = await op()
        except Exception:
            self.record_failure()
            raise
        except BaseException:
            self.release_probe()
            raise
        self.record_success()
        return result


# ---------------------------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------------------------


class RateLimiter:
    """A token bucket (``requests_per_second``, burst of the same size) plus a concurrency cap.

    Use as ``async with limiter:`` around each request. Tokens refill continuously; a caller
    that finds the bucket empty sleeps until the next token is due, so sustained throughput is
    at most ``requests_per_second`` and at most ``max_concurrency`` requests are in flight.
    """

    __slots__ = (
        "_capacity",
        "_clock",
        "_in_flight",
        "_lock",
        "_rate",
        "_semaphore",
        "_sleep",
        "_tokens",
        "_updated",
    )

    def __init__(
        self,
        requests_per_second: float,
        max_concurrency: int,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if requests_per_second <= 0:
            msg = "requests_per_second must be positive"
            raise ValueError(msg)
        if max_concurrency < 1:
            msg = "max_concurrency must be at least 1"
            raise ValueError(msg)
        self._rate = float(requests_per_second)
        self._capacity = max(1.0, float(requests_per_second))
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    async def _take_token(self) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._rate)

    async def acquire(self) -> None:
        await self._semaphore.acquire()
        try:
            await self._take_token()
        except BaseException:
            self._semaphore.release()
            raise
        self._in_flight += 1

    def release(self) -> None:
        self._in_flight -= 1
        self._semaphore.release()

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
