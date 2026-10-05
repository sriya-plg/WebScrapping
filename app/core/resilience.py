"""Retries w/ backoff, per-carrier pacing, circuit breaker, and error taxonomy."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class TransientError(Exception):
    """Timeouts, 5xx, missing response: worth retrying."""


class BlockedError(Exception):
    """Bot protection / 403 / 429: do NOT hammer; counts toward the circuit breaker."""


class NotFoundError(Exception):
    """Reference doesn't exist at the carrier (yet). Not an error for the batch."""


class ManualInterventionRequired(Exception):
    def __init__(self, reason: str, url: str | None = None):
        super().__init__(reason)
        self.reason, self.url = reason, url


async def retry(fn: Callable[[], Awaitable[T]], *, attempts: int = 3, base: float = 2.0, cap: float = 60.0,
                retry_on: tuple[type[Exception], ...] = (TransientError,)) -> T:
    """Exponential backoff with full jitter."""
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on:
            if i == attempts:
                raise
            await asyncio.sleep(random.uniform(0, min(cap, base * 2 ** (i - 1))))
    raise RuntimeError("unreachable")


class Pacer:
    """Per-carrier concurrency cap + randomized spacing between request starts (human-like)."""

    def __init__(self, max_concurrency: int, min_delay: float, max_delay: float):
        self._sem = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self._next = 0.0
        self.min_delay, self.max_delay = min_delay, max_delay

    async def __aenter__(self):
        await self._sem.acquire()
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + random.uniform(self.min_delay, max(self.min_delay, self.max_delay))
        if start > now:
            await asyncio.sleep(start - now)
        return self

    async def __aexit__(self, *exc):
        self._sem.release()


class CircuitBreaker:
    """closed -> open after N consecutive blocks -> half-open probe after cooldown."""

    def __init__(self, threshold: int, cooldown_s: int, now=time.time):
        self.threshold, self.cooldown_s, self._now = threshold, cooldown_s, now
        self.failures, self.opened_at = 0, None

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None and self._now() - self.opened_at < self.cooldown_s

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self._now() - self.opened_at >= self.cooldown_s:  # half-open: let one probe through
            self.opened_at, self.failures = None, self.threshold - 1
            return True
        return False

    def record_success(self) -> None:
        self.failures, self.opened_at = 0, None

    def record_block(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self._now()
