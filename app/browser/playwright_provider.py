"""Playwright provider. engine='playwright' (+playwright-stealth) or engine='patchright' (undetected fork,
same API). Prefers capturing the JSON the page itself fetches over DOM scraping."""
from __future__ import annotations

import asyncio
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.browser.base import (
    DEFAULT_BLOCK_MARKERS,
    BrowserProvider,
    BrowserSession,
    CapturedResponse,
)
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


_DISCOVER_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                      return r.width > 20 && r.height > 10 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const kw = /(\bpro\b|bol|track|search|number|reference|shipment|waybill|housebill|bill of lading)/i;
  const css = el => {
    if (el.id) return '#' + CSS.escape(el.id);
    for (const a of ['name', 'data-testid', 'data-test', 'aria-label', 'placeholder']) {
      const v = el.getAttribute(a);
      if (v) { const sel = el.tagName.toLowerCase() + '[' + a + '="' + v.replace(/"/g, '\\"') + '"]';
               if (document.querySelectorAll(sel).length === 1) return sel; }
    }
    const path = []; let n = el;
    while (n && n.nodeType === 1 && path.length < 6) {
      let i = 1, s = n; while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++;
      path.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + i + ')'); n = n.parentElement; }
    return path.join('>');
  };
  const bad = ['hidden', 'checkbox', 'radio', 'submit', 'button', 'password', 'file', 'email'];
  const inputs = [...document.querySelectorAll('input,textarea')]
      .filter(el => !bad.includes((el.type || 'text').toLowerCase()) && vis(el) && !el.disabled);
  let best = null, score = -1e9;
  for (const el of inputs) {
    const txt = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                 el.labels && el.labels[0] && el.labels[0].innerText].join(' ');
    let sc = kw.test(txt) ? 5 : 0;
    if (el.closest('form')) sc += 1;
    if (el.type === 'search') sc += 1;
    sc -= el.getBoundingClientRect().top / 5000;
    if (sc > score) { score = sc; best = el; }
  }
  if (!best) return null;
  const scope = best.closest('form') || document;
  const btns = [...scope.querySelectorAll('button,input[type=submit],[role=button]')].filter(vis);
  const sb = btns.find(b => /track|search|submit|go|find/i.test(b.innerText || b.value || b.getAttribute('aria-label') || '')) || btns[0] || null;
  return {input: css(best), submit: sb ? css(sb) : null};
}"""


class _PWSession(BrowserSession):
    def __init__(self, context, page, state_path: Path | None, nav_timeout_s: float):
        self.context, self.page, self.state_path = context, page, state_path
        page.set_default_timeout(nav_timeout_s * 1000)

    def _target(self, selector: str, frame: str | None = None):
        if frame:
            return self.page.frame_locator(frame).locator(selector)
        return self.page.locator(selector)

    async def goto(self, url):
        # Carrier pages can stall before DOMContentLoaded while analytics or other
        # third-party resources load. Wait for the document response to commit here;
        # the adapter subsequently waits for the actual tracking form before use.
        await self.page.goto(url, wait_until="commit")
        await asyncio.sleep(random.uniform(0.8, 2.2))

    async def fill(self, selector, value, human=True, frame=None):
        target = self._target(selector, frame).first
        await target.click()
        if human:
            await target.fill("")
            if hasattr(target, "press_sequentially"):
                await target.press_sequentially(value, delay=random.randint(45, 140))
            else:
                await target.type(value, delay=random.randint(45, 140))
        else:
            await target.fill(value)

    async def click(self, selector, frame=None):
        await asyncio.sleep(random.uniform(0.3, 0.9))
        target = self._target(selector, frame).first
        await target.click()

    async def press(self, selector, key, frame=None):
        await asyncio.sleep(random.uniform(0.3, 0.9))
        target = self._target(selector, frame).first
        await target.press(key)

    async def select_option(self, selector, value, frame=None):
        target = self._target(selector, frame).first
        await target.select_option(value)

    async def wait_for_selector(self, selector, timeout_s=20, frame=None):
        try:
            target = self._target(selector, frame).first
            await target.wait_for(state="attached", timeout=timeout_s * 1000)
            return True
        except Exception:
            return False

    async def is_visible(self, selector, frame=None):
        try:
            target = self._target(selector, frame).first
            return await target.is_visible()
        except Exception:
            return False

    async def evaluate(self, expression, arg=None):
        if arg is not None:
            return await self.page.evaluate(expression, arg)
        return await self.page.evaluate(expression)

    async def discover_search_box(self):
        try:
            return await self.page.evaluate(_DISCOVER_JS)
        except Exception:
            return None

    async def capture_all(
        self,
        action,
        timeout_s=30,
        settle_s=1.0,
        until=None,
    ):
        found: list[CapturedResponse] = []
        tasks: list[asyncio.Task] = []
        last = [time.monotonic()]

        async def handle(resp):
            content_type = (
                resp.headers.get("content-type") or ""
            ).lower()

            # Temporary diagnostics
            print(
                f"[NETWORK] {resp.status} "
                f"{resp.request.method} "
                f"{resp.url} "
                f"content-type={content_type}"
            )

            # We only want responses that are likely to contain JSON.
            is_json = (
                "json" in content_type
                or content_type.endswith("+json")
            )

            if not is_json:
                return

            try:
                data = await resp.json()
            except Exception as exc:
                print(
                    f"[NETWORK] JSON parse failed: "
                    f"{resp.url} ({exc})"
                )
                return

            found.append(
                CapturedResponse(
                    resp.url,
                    resp.status,
                    data,
                )
            )

            last[0] = time.monotonic()

            print(
                f"[JSON CAPTURED] {resp.status} {resp.url}"
            )

        listener = lambda resp: tasks.append(
            asyncio.create_task(handle(resp))
        )

        self.page.on("response", listener)

        try:
            print("[ACTION] Starting action")

            await action()

            print("[ACTION] Action completed")

            start = time.monotonic()

            while time.monotonic() - start < timeout_s:
                await asyncio.sleep(0.25)

                quiet = (
                    time.monotonic() - last[0]
                    >= settle_s
                )

                if until and until(found):
                    print(
                        f"[CAPTURE] Matching response found "
                        f"after {len(found)} JSON responses"
                    )
                    await asyncio.sleep(settle_s)
                    break

                if until is None and found and quiet:
                    break

            if not found:
                print(
                    f"[CAPTURE] No JSON responses captured "
                    f"within {timeout_s}s"
                )

        finally:
            self.page.remove_listener(
                "response",
                listener,
            )

            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        print(
            f"[CAPTURE] Returning {len(found)} JSON responses"
        )

        return found
    async def looks_blocked(self, markers=None):
        try:
            text = ((await self.page.title()) + " " + (await self.page.inner_text("body"))[:2000]).lower()
        except Exception:
            return False
        return any(m in text for m in (markers or DEFAULT_BLOCK_MARKERS))

    async def save_state(self):
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            await self.context.storage_state(path=str(self.state_path))
            os.chmod(self.state_path, 0o600)

    async def close(self):
        try:
            await self.save_state()
        except Exception:
            pass
        finally:
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
