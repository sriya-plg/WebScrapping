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


_DISCOVER_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                      return r.width > 20 && r.height > 10 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const kw = /(\bpro\b|bol|track|search|number|reference|shipment|waybill|housebill|bill of lading|tracking numbers)/i;
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
        return self.page.locator(selector).first

    async def goto(self, url):
        await self.page.goto(url, wait_until="domcontentloaded")
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

    async def type_text(self, selector, text, delay_ms=0, clear=False, frame=None):
        target = self._target(selector, frame).first
        if clear:
            await target.click()
            await target.fill("")
        else:
            await target.focus()
        if not text:
            return
        if delay_ms > 0:
            if hasattr(target, "press_sequentially"):
                await target.press_sequentially(text, delay=delay_ms)
            else:
                await target.type(text, delay=delay_ms)
        else:
            if clear:
                await target.fill(text)
            else:
                if hasattr(target, "press_sequentially"):
                    await target.press_sequentially(text)
                else:
                    await target.type(text)

    async def get_value(self, selector, frame=None):
        try:
            target = self._target(selector, frame).first
            return await target.input_value()
        except Exception:
            return ""

    async def set_input_value(self, selector, value, frame=None):
        """Set textarea/input value and notify React/Vue controlled components."""
        target = self._target(selector, frame).first
        await target.click()

        async def _apply() -> None:
            await target.evaluate(
                """(el, v) => {
                    el.focus();
                    el.value = v;
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertFromPaste' }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )

        norm = lambda t: (t or "").replace("\r\n", "\n")
        await _apply()
        if norm(await target.input_value()) != norm(value):
            await target.fill(value)
        if norm(await target.input_value()) != norm(value):
            await _apply()

    async def click(self, selector, frame=None, force=False):
        await asyncio.sleep(random.uniform(0.3, 0.9))
        target = self._target(selector, frame).first
        await target.click(force=force)

    async def press(self, selector, key, frame=None):
        await asyncio.sleep(random.uniform(0.3, 0.9))
        target = self._target(selector, frame).first
        await target.press(key)

    async def wait_ready(self, submit_selector: str | None, timeout_s: float = 10) -> None:
        """Wait until submit control is visible and enabled (Angular forms e.g. Estes)."""
        if not submit_selector:
            return
        target = self.page.locator(submit_selector).first
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if await target.is_visible() and await target.is_enabled():
                    return
            except Exception:
                pass
            await asyncio.sleep(0.25)

    async def select_option(self, selector, value, frame=None):
        target = self._target(selector, frame).first
        await target.select_option(value)

    async def wait_for_selector(self, selector, timeout_s=20, frame=None):
        try:
            root = self.page.frame_locator(frame) if frame else self.page
            target = root.locator(selector)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                count = await target.count()
                for index in range(count):
                    item = target.nth(index)
                    try:
                        if await item.is_visible():
                            return True
                    except Exception:
                        continue
                await asyncio.sleep(0.25)
            return False
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

            # We only want responses that are likely to contain JSON.
            is_json = (
                "json" in content_type
                or content_type.endswith("+json")
            )

            if not is_json:
                return

            try:
                data = await resp.json()
            except Exception:
                return

            found.append(
                CapturedResponse(
                    resp.url,
                    resp.status,
                    data,
                )
            )

            last[0] = time.monotonic()


        listener = lambda resp: tasks.append(
            asyncio.create_task(handle(resp))
        )

        self.page.on("response", listener)

        try:
            await action()

            start = time.monotonic()

            while time.monotonic() - start < timeout_s:
                await asyncio.sleep(0.25)

                quiet = (
                    time.monotonic() - last[0]
                    >= settle_s
                )

                if until and until(found):
                    await asyncio.sleep(settle_s)
                    break
                if until is not None and found and quiet:
                    break

                if until is None and found and quiet:
                    break


        finally:
            self.page.remove_listener(
                "response",
                listener,
            )

            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        return found

    async def save_debug(self, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(target), full_page=True)
        return str(target)

    def current_url(self) -> str:
        return self.page.url
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

        headless = self.cfg.headless
        env_headless = os.environ.get("BROWSER_HEADLESS") or os.environ.get("HEADLESS")
        if env_headless is not None:
            headless = env_headless.lower() in ("true", "1", "yes")
        elif os.name == "posix" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            headless = True

        kw: dict[str, Any] = {"headless": headless}
        if self.cfg.channel:
            kw["channel"] = self.cfg.channel
        if self.cfg.proxy:
            kw["proxy"] = self.cfg.proxy
        if self.engine == "playwright":
            kw["args"] = ["--disable-blink-features=AutomationControlled"]
        try:
            self._browser = await self._pw.chromium.launch(**kw)
        except Exception as exc:
            if kw.get("channel"):
                import logging
                logging.getLogger(__name__).warning(
                    "launching browser with channel=%r failed: %s; falling back to default chromium",
                    kw["channel"], exc,
                )
                kw.pop("channel", None)
                self._browser = await self._pw.chromium.launch(**kw)
            else:
                raise

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
