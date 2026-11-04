"""Scrapling engine provider: AsyncStealthySession over a persistent patchright Chromium context.

The scraper drives a page from `engine.context` directly, so it gets the same
Playwright-API surface (selectors, response listeners, storage) that the patchright
provider provides; only the launcher and the stealth hardening differ.

`solve_cloudflare` is deliberately NOT set: it only acts inside `engine.fetch()`,
which this provider never calls. Cloudflare interstitials are handled at the scraper
level (`_wait_challenge_clear`).

Every failure mode falls back to today's patchright provider with a WARNING that
carries the reason: missing fetchers extras, import errors, a profile locked by
another process (e.g. a concurrent `diagnose` run), or any launch failure. A silent
fallback stays visible through the per-run `engine_used` outcome field.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from app.browser.base import BrowserProvider, BrowserSession
from app.browser.playwright_provider import (
    PlaywrightProvider,
    _PWSession,
    _attach_page_listeners,
    _effective_headless,
)
from app.settings import BrowserConfig

logger = logging.getLogger(__name__)

# A lock older than this is considered abandoned (crashed process) and replaced.
LOCK_STALE_AFTER_S = 24 * 3600


def _load_stealthy_session() -> Any:
    """Import hook (separate so tests can fake missing extras / import errors)."""
    from scrapling.fetchers import AsyncStealthySession  # requires scrapling[fetchers] extras
    return AsyncStealthySession


def _safe_name(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)


class ScraplingProvider(BrowserProvider):
    """StealthySession (patchright Chromium, persistent profile) with patchright fallback."""

    def __init__(self, cfg: BrowserConfig, state_dir: str, fallback: BrowserProvider | None = None):
        self.cfg, self.state_dir = cfg, Path(state_dir)
        # Exactly today's production path, used for every fallback case.
        self._fallback = fallback or PlaywrightProvider(cfg, state_dir, engine="patchright", stealth=False)
        self._engine: Any = None
        self._lock_path: Path | None = None
        self._state_reused = False

    async def _acquire_lock(self, lock_path: Path) -> bool:
        """Create the profile lock exclusively; stale locks (>24h) are replaced."""
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    return False
                if age < LOCK_STALE_AFTER_S:
                    return False
                logger.warning("replacing stale scrapling profile lock %s (age %.0fs)", lock_path, age)
                try:
                    lock_path.unlink()
                except OSError:
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            self._lock_path = lock_path
            return True

    def _release_lock(self) -> None:
        if self._lock_path is not None:
            try:
                self._lock_path.unlink()
            except OSError:
                logger.warning("could not release scrapling profile lock %s", self._lock_path)
            self._lock_path = None

    async def _start_engine(self, key: str) -> Any:
        """Start AsyncStealthySession once; returns the engine or raises."""
        if self._engine is not None:
            return self._engine
        stealthy_session = _load_stealthy_session()

        user_data_dir = self.state_dir / _safe_name(f"scrapling_{key}")
        self._state_reused = user_data_dir.exists()
        lock_path = Path(f"{user_data_dir}.lock")
        if not await self._acquire_lock(lock_path):
            lock_pid = ""
            try:
                lock_pid = lock_path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            raise RuntimeError(
                f"scrapling profile for {key} is in use by another process "
                f"(pid {lock_pid or 'unknown'} holds {lock_path})"
            )

        kwargs: dict[str, Any] = dict(
            headless=_effective_headless(self.cfg),
            locale=self.cfg.locale,
            timezone_id=self.cfg.timezone_id,
            timeout=int(self.cfg.nav_timeout_s * 1000),
            user_data_dir=str(user_data_dir),
        )
        if self.cfg.proxy:
            kwargs["proxy"] = self.cfg.proxy
        if (self.cfg.channel or "").lower() == "chrome":
            kwargs["real_chrome"] = True
        # Keep the profile's configured viewport instead of StealthySession's default.
        additional = {"viewport": {"width": self.cfg.viewport_w, "height": self.cfg.viewport_h}}
        additional.update((self.cfg.scrapling_options or {}).get("additional_args") or {})
        kwargs["additional_args"] = additional
        # BrowserConfig.scrapling_options is a StealthConfig passthrough (highest priority).
        for option, value in (self.cfg.scrapling_options or {}).items():
            if option != "additional_args":
                kwargs[option] = value

        engine = stealthy_session(**kwargs)
        await engine.start()
        self._engine = engine
        logger.info(
            "scrapling engine started | key=%s user_data_dir=%s headless=%s real_chrome=%s state_reused=%s",
            key, user_data_dir, kwargs["headless"], kwargs.get("real_chrome", False), self._state_reused,
        )
        return engine

    async def open_session(self, key: str) -> BrowserSession:
        try:
            engine = await self._start_engine(key)
        except ModuleNotFoundError as exc:
            logger.warning(
                "scrapling[fetchers] extras not installed (%s); using patchright fallback for %s",
                exc, key)
            return await self._fallback.open_session(key)
        except Exception as exc:
            logger.warning(
                "scrapling engine failed to start for %s (%s: %s); using patchright fallback",
                key, type(exc).__name__, exc)
            return await self._fallback.open_session(key)
        try:
            page = await engine.context.new_page()
        except Exception as exc:
            logger.warning(
                "scrapling engine could not open a page for %s (%s: %s); using patchright fallback",
                key, type(exc).__name__, exc)
            await self.close()
            return await self._fallback.open_session(key)
        _attach_page_listeners(page)
        # state_path=None: persistence is the persistent context's job (user_data_dir).
        return _PWSession(
            engine.context,
            page,
            None,
            self.cfg.nav_timeout_s,
            engine_name="scrapling",
            state_reused=self._state_reused,
        )

    async def close(self) -> None:
        if self._engine is not None:
            engine, self._engine = self._engine, None
            try:
                await engine.close()
            except Exception as exc:
                logger.warning("error stopping scrapling engine: %s", exc)
        self._release_lock()
        await self._fallback.close()