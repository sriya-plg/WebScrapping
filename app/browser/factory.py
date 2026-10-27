from __future__ import annotations

from app.browser.base import BrowserProvider
from app.settings import BrowserConfig


def make_provider(cfg: BrowserConfig, state_dir: str) -> BrowserProvider:
    mode = cfg.browser_mode or cfg.provider
    if mode == "playwright_stealth":
        from app.browser.playwright_provider import PlaywrightProvider
        return PlaywrightProvider(cfg, state_dir, engine="playwright", stealth=True)
    if mode == "patchright":
        from app.browser.playwright_provider import PlaywrightProvider
        return PlaywrightProvider(cfg, state_dir, engine="patchright", stealth=False)
    if mode == "crawl4ai":
        from app.browser.crawl4ai_provider import Crawl4AIProvider
        return Crawl4AIProvider(cfg, state_dir)
    if mode == "browser_use":
        from app.browser.browser_use_provider import BrowserUseProvider
        return BrowserUseProvider(cfg, state_dir)
    raise ValueError(f"unknown browser provider {mode!r}")
