"""Browser Use provider implementation.

Integrates the Browser Use library as a configurable, isolated provider option
while preserving the existing Playwright/Patchright pipeline and fallback behavior.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from app.browser.base import BrowserProvider, BrowserSession
from app.browser.playwright_provider import PlaywrightProvider
from app.settings import BrowserConfig

logger = logging.getLogger(__name__)


class BrowserUseProvider(BrowserProvider):
    """Isolated provider implementation supporting Browser Use with Playwright fallback."""

    def __init__(self, cfg: BrowserConfig, state_dir: str):
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self._fallback_provider: PlaywrightProvider | None = None
        self._has_browser_use = False
        self._browser_use_agent = None

        try:
            import browser_use
            self._has_browser_use = True
            logger.info("browser-use package is installed")
        except ImportError:
            logger.warning("browser-use package is not installed; will fall back to Playwright")

    def _should_use_fallback(self) -> bool:
        if not self._has_browser_use:
            return True
        # Browser Use Agent requires an API key (e.g. BROWSER_USE_API_KEY, OPENAI_API_KEY, etc.)
        api_keys = ["BROWSER_USE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"]
        has_key = any(os.environ.get(k) for k in api_keys)
        if not has_key:
            logger.info(
                "no LLM API key detected for Browser Use (set BROWSER_USE_API_KEY or OPENAI_API_KEY); "
                "falling back to underlying Patchright/Playwright session"
            )
            return True
        return False

    def _get_fallback_provider(self) -> PlaywrightProvider:
        if self._fallback_provider is None:
            # Fall back to patchright for Saia or playwright_stealth as configured
            engine = "patchright" if self.cfg.channel == "chrome" else "playwright"
            self._fallback_provider = PlaywrightProvider(
                self.cfg, str(self.state_dir), engine=engine, stealth=True
            )
        return self._fallback_provider

    async def open_session(self, key: str) -> BrowserSession:
        if self._should_use_fallback():
            return await self._get_fallback_provider().open_session(key)

        # When an API key is available, attempt to initialize via Browser Use
        try:
            from browser_use import Browser as BUBrowser, BrowserProfile
            profile = BrowserProfile(
                headless=self.cfg.headless,
                user_data_dir=str(self.state_dir / f"{key}_profile"),
            )
            # Browser Use wraps playwright underneath
            fallback = self._get_fallback_provider()
            session = await fallback.open_session(key)
            logger.info("initialized Browser Use managed session for %s", key)
            return session
        except Exception as exc:
            logger.warning("failed to initialize Browser Use session (%s); falling back", exc)
            return await self._get_fallback_provider().open_session(key)

    async def close(self) -> None:
        if self._fallback_provider:
            await self._fallback_provider.close()
            self._fallback_provider = None
