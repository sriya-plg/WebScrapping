from __future__ import annotations

from app.browser.base import BrowserProvider
from app.settings import BrowserConfig


def make_provider(cfg: BrowserConfig, state_dir: str) -> BrowserProvider:
    """Playwright is the default because response-event interception is a core requirement.

    Patchright is retained for carriers where its Chromium patches are required. Crawl4AI is
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
    raise ValueError(f"unsupported browser provider {mode!r}; use playwright_stealth or patchright")
