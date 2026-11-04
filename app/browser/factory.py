from __future__ import annotations

import logging

from app.browser.base import BrowserProvider
from app.settings import BrowserConfig

logger = logging.getLogger(__name__)


def make_provider(cfg: BrowserConfig, state_dir: str) -> BrowserProvider:
    """Playwright is the default because response-event interception is a core requirement.

    Patchright is retained for carriers where its Chromium patches are required. Scrapling
    (AsyncStealthySession over patchright Chromium with extra stealth hardening) is opt-in
    per carrier via browser_mode (e.g. SAIA_BROWSER_MODE=scrapling) and falls back to
    patchright when its extras are missing or the engine fails to launch. Crawl4AI is
    not selected because the existing integration is a stub and it is not a drop-in replacement
    for the current persistent browser session + response-listener contract.
    """
    mode = cfg.browser_mode or cfg.provider
    if mode == "playwright_stealth":
        from app.browser.playwright_provider import PlaywrightProvider
        return PlaywrightProvider(cfg, state_dir, engine="playwright", stealth=True)
    if mode == "patchright":
        from app.browser.playwright_provider import PlaywrightProvider
        return PlaywrightProvider(cfg, state_dir, engine="patchright", stealth=False)
    if mode == "scrapling":
        try:
            from app.browser.scrapling_provider import ScraplingProvider
        except Exception as exc:
            logger.warning("scrapling provider unavailable (%s); using patchright", exc)
            from app.browser.playwright_provider import PlaywrightProvider
            return PlaywrightProvider(cfg, state_dir, engine="patchright", stealth=False)
        return ScraplingProvider(cfg, state_dir)
    raise ValueError(f"unsupported browser provider {mode!r}; use playwright_stealth, patchright or scrapling")