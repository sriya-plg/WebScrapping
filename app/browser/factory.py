from __future__ import annotations

from app.browser.base import BrowserProvider
from app.settings import BrowserConfig


def make_provider(cfg: BrowserConfig, state_dir: str) -> BrowserProvider:
    if cfg.provider == "playwright_stealth":
        from app.browser.playwright_provider import PlaywrightProvider
        return PlaywrightProvider(cfg, state_dir, engine="playwright", stealth=True)
    if cfg.provider == "patchright":
        from app.browser.playwright_provider import PlaywrightProvider
        return PlaywrightProvider(cfg, state_dir, engine="patchright", stealth=False)
    if cfg.provider == "crawl4ai":
        from app.browser.crawl4ai_provider import Crawl4AIProvider
        return Crawl4AIProvider(cfg, state_dir)
    raise ValueError(f"unknown browser provider {cfg.provider!r}")
