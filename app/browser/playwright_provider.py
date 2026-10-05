"""Playwright provider. engine='playwright' (+playwright-stealth) or engine='patchright' (undetected fork,
same API). Prefers capturing the JSON the page itself fetches over DOM scraping."""
from __future__ import annotations

import asyncio
import os
import random
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.browser.base import DEFAULT_BLOCK_MARKERS, BrowserProvider, BrowserSession, CapturedResponse
from app.settings import BrowserConfig


async def _apply_stealth(context) -> None:
    try:  # playwright-stealth >= 2.x
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(context)
        return
    except ImportError:
        pass
    try:  # 1.x
        from playwright_stealth import stealth_async
        context.on("page", lambda p: asyncio.create_task(stealth_async(p)))
    except ImportError:
        pass


class _PWSession(BrowserSession):
    def __init__(self, context, page, state_path: Path | None, nav_timeout_s: float):
        self.context, self.page, self.state_path = context, page, state_path
        page.set_default_timeout(nav_timeout_s * 1000)

    async def goto(self, url):
        await self.page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(0.8, 2.2))

    async def fill(self, selector, value, human=True):
        await self.page.click(selector)
        if human:
            await self.page.fill(selector, "")
            await self.page.type(selector, value, delay=random.randint(45, 140))
        else:
            await self.page.fill(selector, value)

    async def click(self, selector):
        await asyncio.sleep(random.uniform(0.3, 0.9))
        await self.page.click(selector)

    async def select_option(self, selector, value):
        await self.page.select_option(selector, value)

    async def wait_for_selector(self, selector, timeout_s=20):
        try:
            await self.page.wait_for_selector(selector, timeout=timeout_s * 1000)
            return True
        except Exception:
            return False

    async def capture_json(self, url_regex, action: Callable[[], Awaitable[Any]], timeout_s=30):
        rx, found, tasks, done = re.compile(url_regex), [], [], asyncio.Event()

        async def handle(resp):
            if not rx.search(resp.url):
                return
            try:
                data = await resp.json()
            except Exception:
                data = None
            found.append(CapturedResponse(resp.url, resp.status, data))
            done.set()

        listener = lambda resp: tasks.append(asyncio.create_task(handle(resp)))  # noqa: E731
        self.page.on("response", listener)
        try:
            await action()
            await asyncio.wait_for(done.wait(), timeout_s)
            await asyncio.sleep(0.3)
        except asyncio.TimeoutError:
            pass
        finally:
            self.page.remove_listener("response", listener)
            await asyncio.gather(*tasks, return_exceptions=True)
        return found

    async def looks_blocked(self, markers=None):
        text = ((await self.page.title()) + " " + (await self.page.inner_text("body"))[:2000]).lower()
        return any(m in text for m in (markers or DEFAULT_BLOCK_MARKERS))

    async def save_state(self):
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            await self.context.storage_state(path=str(self.state_path))
            os.chmod(self.state_path, 0o600)

    async def close(self):
        await self.save_state()
        await self.context.close()


class PlaywrightProvider(BrowserProvider):
    def __init__(self, cfg: BrowserConfig, state_dir: str, engine: str = "playwright", stealth: bool = True):
        self.cfg, self.state_dir, self.engine, self.stealth = cfg, Path(state_dir), engine, stealth
        self._pw = self._browser = None

    async def _start(self):
        if self._browser:
            return
        if self.engine == "patchright":
            from patchright.async_api import async_playwright
        else:
            from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        kw: dict[str, Any] = {"headless": self.cfg.headless}
        if self.cfg.channel:
            kw["channel"] = self.cfg.channel
        if self.cfg.proxy:
            kw["proxy"] = self.cfg.proxy
        if self.engine == "playwright":
            kw["args"] = ["--disable-blink-features=AutomationControlled"]
        self._browser = await self._pw.chromium.launch(**kw)

    async def open_session(self, key: str) -> BrowserSession:
        await self._start()
        state = self.state_dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', key)}.json"
        ctx_kw: dict[str, Any] = dict(locale=self.cfg.locale, timezone_id=self.cfg.timezone_id,
                                      viewport={"width": self.cfg.viewport_w, "height": self.cfg.viewport_h})
        if self.cfg.persist_state and state.exists():
            ctx_kw["storage_state"] = str(state)
        context = await self._browser.new_context(**ctx_kw)
        if self.stealth and self.engine == "playwright":
            await _apply_stealth(context)
        page = await context.new_page()
        return _PWSession(context, page, state if self.cfg.persist_state else None, self.cfg.nav_timeout_s)

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None
