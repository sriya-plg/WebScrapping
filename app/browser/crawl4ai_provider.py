"""Fallback slot for Saia if patchright gets blocked. Crawl4AI is crawl/extract oriented (hooks, undetected
adapter, session_id, capture_network_requests) -- map open_session/goto/capture_json onto its AsyncWebCrawler
hooks here. Intentionally a stub: implement only if Saia blocks the patchright provider."""
from __future__ import annotations

from app.browser.base import BrowserProvider


class Crawl4AIProvider(BrowserProvider):
    def __init__(self, cfg, state_dir):
        raise NotImplementedError("Crawl4AIProvider is a fallback stub; see docstring")

    async def open_session(self, key):  # pragma: no cover
        raise NotImplementedError

    async def close(self):  # pragma: no cover
        raise NotImplementedError
