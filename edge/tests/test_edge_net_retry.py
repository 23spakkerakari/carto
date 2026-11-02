"""carto_edge.net.retry: jittered exponential backoff, async retry, circuit breaker, rate limit
(spec 8.1 "Timeouts, retries with jittered exponential backoff, circuit breaker per source.
Rate limits per source (requests/s and concurrent queries)").
"""

from __future__ import annotations

import asyncio

import pytest

from carto_edge.net.retry import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RateLimiter,
    backoff_delay,
    backoff_delays,
    retry_async,
)

# ---------------------------------------------------------------------------------------------
# backoff
# ---------------------------------------------------------------------------------------------


def test_backoff_without_jitter_doubles_and_caps() -> None:
    assert backoff_delays(6, base=1.0, cap=10.0, jitter=0.0) == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]


def test_backoff_delay_single_attempt() -> None:
    assert backoff_delay(1, base=0.5, cap=8.0, jitter=0.0) == 0.5
    assert backoff_delay(4, base=0.5, cap=8.0, jitter=0.0) == 4.0
    assert backoff_delay(40, base=0.5, cap=8.0, jitter=0.0) == 8.0


def test_backoff_jitter_stays_within_band() -> None:
    values = [0.0, 0.5, 1.0]
    calls = iter(values * 10)
    delays = backoff_delays(3, base=1.0, cap=100.0, jitter=0.5, rng=lambda: next(calls))
    # rng 0.0 -> lower edge, 0.5 -> nominal, 1.0 -> upper edge
    assert delays == [0.5, 2.0, 6.0]


def test_backoff_never_negative_or_above_cap() -> None:
    delays = backoff_delays(8, base=1.0, cap=5.0, jitter=1.0, rng=lambda: 1.0)
    assert all(0.0 <= delay <= 5.0 for delay in delays)
    delays = backoff_delays(8, base=1.0, cap=5.0, jitter=1.0, rng=lambda: 0.0)
    assert all(0.0 <= delay <= 5.0 for delay in delays)


@pytest.mark.parametrize(
    ("attempts", "base", "cap", "jitter"),
    [(0, 1, 1, 0), (1, -1, 1, 0), (1, 1, 0.5, 0), (1, 1, 2, 2)],
)
def test_backoff_rejects_bad_inputs(attempts: int, base: float, cap: float, jitter: float) -> None:
    with pytest.raises(ValueError, match="backoff"):
        backoff_delays(attempts, base=base, cap=cap, jitter=jitter)


# ---------------------------------------------------------------------------------------------
# retry_async
# ---------------------------------------------------------------------------------------------


async def test_retry_returns_first_success() -> None:
    calls = 0
    slept: list[float] = []

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("flaky")
        return "ok"

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    result = await retry_async(
        op, attempts=5, base=1.0, cap=4.0, retry_on=(ConnectionError,), jitter=0.0, sleep=sleep
    )
    assert result == "ok"
    assert calls == 3
    assert slept == [1.0, 2.0]


async def test_retry_gives_up_after_attempts_and_raises_last() -> None:
    calls = 0

    async def op() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError(f"attempt {calls}")

    async def sleep(_seconds: float) -> None:
        return None

    with pytest.raises(ConnectionError, match="attempt 3"):
        await retry_async(
            op, attempts=3, base=0.1, cap=1.0, retry_on=(ConnectionError,), sleep=sleep
        )
    assert calls == 3


async def test_retry_does_not_retry_other_exceptions() -> None:
    calls = 0

    async def op() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("fatal")

    with pytest.raises(ValueError, match="fatal"):
        await retry_async(op, attempts=3, base=0.0, cap=0.0, retry_on=(ConnectionError,))
    assert calls == 1


async def test_retry_calls_on_retry_hook() -> None:
    seen: list[tuple[int, float]] = []
    calls = 0

    async def op() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError
        return calls

    async def sleep(_seconds: float) -> None:
        return None

    def on_retry(attempt: int, delay: float, exc: BaseException) -> None:
        assert isinstance(exc, TimeoutError)
        seen.append((attempt, delay))

    assert (
        await retry_async(
            op,
            attempts=2,
            base=0.25,
            cap=1.0,
            retry_on=(TimeoutError,),
            jitter=0.0,
            sleep=sleep,
            on_retry=on_retry,
        )
        == 2
    )
    assert seen == [(1, 0.25)]


async def test_retry_rejects_zero_attempts() -> None:
    async def op() -> None:
        return None

    with pytest.raises(ValueError, match="attempts"):
        await retry_async(op, attempts=0, base=0.1, cap=1.0, retry_on=(Exception,))


# ---------------------------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------------------------


def state(breaker: CircuitBreaker) -> CircuitState:
    return breaker.state


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_breaker_opens_after_threshold_and_half_opens_after_reset() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=30.0, clock=clock)
    assert state(breaker) is CircuitState.CLOSED
    for _ in range(2):
        breaker.record_failure()
    assert state(breaker) is CircuitState.CLOSED
    assert breaker.allow()
    breaker.record_failure()
    assert state(breaker) is CircuitState.OPEN
    assert not breaker.allow()
    with pytest.raises(CircuitOpenError):
        breaker.check()
    clock.now += 29.0
    assert not breaker.allow()
    clock.now += 1.5
    # One probe is let through in the half-open state, the next is refused until it reports.
    assert state(breaker) is CircuitState.HALF_OPEN
    assert breaker.allow()
    assert not breaker.allow()
    breaker.record_success()
    assert state(breaker) is CircuitState.CLOSED
    assert breaker.failures == 0
    assert breaker.allow()


def test_breaker_half_open_failure_reopens() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10.0, clock=clock)
    breaker.record_failure()
    assert state(breaker) is CircuitState.OPEN
    clock.now += 10.0
    assert breaker.allow()
    breaker.record_failure()
    assert state(breaker) is CircuitState.OPEN
    assert not breaker.allow()
    clock.now += 10.0
    assert breaker.allow()


def test_breaker_success_resets_failure_count_when_closed() -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=1.0, clock=Clock())
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert state(breaker) is CircuitState.CLOSED


async def test_breaker_call_wraps_an_operation() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=5.0, clock=clock)

    async def boom() -> None:
        raise ConnectionError

    async def fine() -> str:
        return "fine"

    with pytest.raises(ConnectionError):
        await breaker.call(boom)
    with pytest.raises(CircuitOpenError):
        await breaker.call(fine)
    clock.now += 5.0
    assert await breaker.call(fine) == "fine"
    assert state(breaker) is CircuitState.CLOSED


async def test_breaker_cancelled_half_open_probe_frees_the_probe_slot() -> None:
    """A probe cancelled mid-flight (a poll stopped at shutdown or by a timeout) must not leave
    the breaker half-open with a probe "in flight" forever; cancellation is not a failure."""
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10.0, clock=clock)

    async def boom() -> None:
        raise ConnectionError

    with pytest.raises(ConnectionError):
        await breaker.call(boom)
    clock.now += 10.0
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(breaker.call(slow))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert breaker.failures == 1  # the cancelled probe was not counted
    assert state(breaker) is CircuitState.HALF_OPEN

    async def fine() -> str:
        return "fine"

    assert await breaker.call(fine) == "fine"
    assert state(breaker) is CircuitState.CLOSED


async def test_breaker_cancelled_call_while_closed_counts_nothing() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10.0, clock=Clock())

    async def slow() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(breaker.call(slow))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state(breaker) is CircuitState.CLOSED
    assert breaker.failures == 0


def test_breaker_rejects_bad_parameters() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreaker(failure_threshold=0, reset_seconds=1.0)
    with pytest.raises(ValueError, match="reset_seconds"):
        CircuitBreaker(failure_threshold=1, reset_seconds=0.0)


# ---------------------------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------------------------


async def test_rate_limiter_token_bucket_waits_for_refill() -> None:
    clock = Clock()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    limiter = RateLimiter(requests_per_second=2.0, max_concurrency=4, clock=clock, sleep=sleep)
    # Burst capacity equals the per-second rate: two immediate, the third waits half a second.
    async with limiter:
        pass
    async with limiter:
        pass
    assert slept == []
    async with limiter:
        pass
    assert len(slept) == 1
    assert slept[0] == pytest.approx(0.5)


async def test_rate_limiter_bounds_concurrency() -> None:
    limiter = RateLimiter(requests_per_second=1000.0, max_concurrency=2)
    active = 0
    peak = 0

    async def work() -> None:
        nonlocal active, peak
        async with limiter:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(work() for _ in range(6)))
    assert peak == 2
    assert limiter.in_flight == 0


async def test_rate_limiter_release_on_error() -> None:
    limiter = RateLimiter(requests_per_second=100.0, max_concurrency=1)
    with pytest.raises(RuntimeError):
        async with limiter:
            raise RuntimeError
    assert limiter.in_flight == 0
    async with limiter:
        assert limiter.in_flight == 1


def test_rate_limiter_rejects_bad_parameters() -> None:
    with pytest.raises(ValueError, match="requests_per_second"):
        RateLimiter(requests_per_second=0.0, max_concurrency=1)
    with pytest.raises(ValueError, match="max_concurrency"):
        RateLimiter(requests_per_second=1.0, max_concurrency=0)
