"""Retries w/ backoff, per-carrier pacing, circuit breaker, and error taxonomy."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TransientError(Exception):
    """Timeouts, 5xx, missing response: worth retrying."""


class BlockedError(Exception):
    """Bot protection / 403 / 429: do NOT hammer; counts toward the circuit breaker."""


class NotFoundError(Exception):
    """Reference doesn't exist at the carrier (yet). Not an error for the batch."""


class ChallengeError(Exception):
    """Bot-check or Cloudflare challenge detected."""


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
    """closed -> open after N consecutive blocks -> half-open probe after cooldown.

    With `state_path` + `key`, the failure count and open timestamp persist to a small
    JSON file so a cooldown survives restarts of one-shot scrape processes. The file is
    operational state (like browser storage-state JSON), not shipment data. If two
    scrape processes overlap, the last write wins.
    """

    def __init__(self, threshold: int, cooldown_s: int, now=time.time,
                 state_path: str | Path | None = None, key: str = ""):
        self.threshold, self.cooldown_s, self._now = threshold, cooldown_s, now
        self.state_path = Path(state_path) if state_path else None
        self.key = key
        self.failures, self.opened_at = 0, None
        if self.state_path and self.key:
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8")).get(self.key)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.failures = int(data.get("failures") or 0)
            self.opened_at = data.get("opened_at")

    def _save(self) -> None:
        if not (self.state_path and self.key):
            return
        try:
            data: dict[str, Any] = {}
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                pass
            if self.opened_at is None and not self.failures:
                data.pop(self.key, None)  # fully healthy: drop the section
            else:
                data[self.key] = {"failures": self.failures, "opened_at": self.opened_at}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.warning("could not persist circuit-breaker state to %s: %s", self.state_path, exc)

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None and self._now() - self.opened_at < self.cooldown_s

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self._now() - self.opened_at >= self.cooldown_s:  # half-open: let one probe through
            self.opened_at, self.failures = None, self.threshold - 1
            self._save()
            return True
        return False

    def record_success(self) -> None:
        self.failures, self.opened_at = 0, None
        self._save()

    def record_block(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self._now()
        self._save()
