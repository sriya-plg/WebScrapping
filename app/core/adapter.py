"""Adapter contract + GenericAdapter (the default for every carrier).

GenericAdapter works for sites using Scrapling-based DOM extraction:
  1. Open the tracking URL.
  2. Find and fill the search box.
  3. Submit the tracking reference.
  4. Wait for the tracking result content / DOM to render.
  5. Use Scrapling to extract shipment fields from the page content.
  6. Hand the extracted dictionary to mapper.normalize().
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Protocol

import httpx

from app.browser.base import BrowserProvider, BrowserSession
from app.core import mapper
from app.core.extractor import ScraplingExtractor
from app.core.resilience import BlockedError, ManualInterventionRequired, NotFoundError, TransientError
from app.models import CarrierAccount, TrackingResult
from app.settings import CarrierSpec

# How many times we re-fill + re-click the submit button before falling back to Enter.
SUBMIT_ATTEMPTS = 3
SUBMIT_RETRY_PAUSE_S = 1.5

# Tried in order when site.extra.consent_selectors is not set. "Reject" options first.
DEFAULT_CONSENT_SELECTORS = [
    "#onetrust-reject-all-handler",
    "#CybotCookiebotDialogBodyButtonDecline",
    "button:has-text('Reject All')",
    "button:has-text('Reject all')",
    "button:has-text('Decline')",
    "button:has-text('Do Not Sell')",
    "button:has-text('Do not sell')",
]


class Cache(Protocol):
    def get(self, name: str) -> dict | None: ...
    def set(self, name: str, value: dict) -> None: ...
    def delete(self, name: str) -> None: ...


def session_key(spec: CarrierSpec) -> str:
    """One cookie profile per client+carrier. Used by adapters AND the `assist` command (must match)."""
    return f"{spec.client}_{spec.code}"


class CarrierAdapter(ABC):
    def __init__(
        self,
        spec: CarrierSpec,
        account: CarrierAccount,
        browser: BrowserProvider | None,
        cache: Cache | None = None,
    ):
        self.spec, self.account, self.browser, self.cache = spec, account, browser, cache
        self.session: BrowserSession | None = None
        self.io_lock = asyncio.Lock()  # one page per session -> serialize browser interaction
        self.log = logging.getLogger(f"adapter.{spec.client}.{spec.code}")

    async def authenticate(self) -> None:
        """Open session / log in / clear challenge. Raise BlockedError or ManualInterventionRequired."""

    @abstractmethod
    async def track(self, ref: str, ref_type: str) -> dict:
        """Extracted carrier data for one reference. Raise NotFoundError / TransientError / BlockedError."""

    def normalize(self, raw: dict, ref: str, ref_type: str) -> TrackingResult:
        return mapper.normalize(self.spec, raw, ref, ref_type, self.account)

    async def close(self) -> None:
        if self.session:
            try:
                await self.session.close()
            finally:
                self.session = None


class GenericAdapter(CarrierAdapter):
    # ------------------------------------------------------------------ session
    @property
    def url(self) -> str | None:
        return self.spec.site.tracking_url or self.account.tracking_url

    async def authenticate(self) -> None:
        if self.spec.site.transport == "http":
            return
        if not self.url:
            raise ManualInterventionRequired("no tracking URL (backend trackingUrl empty; set site.tracking_url)")
        self.session = await self.browser.open_session(session_key(self.spec))
        await self.session.goto(self.url)
        await self._on_landing()
        await self._resolve_selectors()

    async def _on_landing(self) -> None:
        """Hook: runs after the tracking page loads. Default: fail fast if we got a block page."""
        if await self.session.looks_blocked(self.spec.block_markers or None):
            raise BlockedError("block page on landing")
        await self._dismiss_consent()

    async def _dismiss_consent(self) -> bool:
        """Click 'reject all' / 'do not sell' on a cookie banner if one appears. Never fails the run."""
        s = self.session
        sels = self.spec.site.extra.get("consent_selectors") or DEFAULT_CONSENT_SELECTORS
        try:
            if not await s.wait_for_selector(", ".join(sels), timeout_s=6):
                return False
        except Exception:
            return False
        for sel in sels:
            try:
                if await s.is_visible(sel):
                    await s.click(sel)
                    self.log.info("dismissed cookie banner via %s", sel)
                    await asyncio.sleep(1)
                    return True
            except Exception as exc:
                self.log.warning("consent click failed for %s: %s", sel, exc)
        return False

    async def _resolve_selectors(self, force: bool = False) -> None:
        s = self.spec.site
        self._sel: dict[str, Any] = {"input": s.input_selector, "submit": s.submit_selector, "source": "config"}
        if s.input_selector:
            return
        cached = None if (force or not self.cache) else self.cache.get("selectors")
        if cached:
            self._sel = {**cached, "source": "cache"}
            return
        found = await self.session.discover_search_box()
        if not found or not found.get("input"):
            raise ManualInterventionRequired(
                "could not find a search box; set site.input_selector in mapping.yaml",
                self.url,
            )
        self._sel = {"input": found["input"], "submit": s.submit_selector or found.get("submit"), "source": "auto"}

    # ------------------------------------------------------------------ tracking
    async def track(self, ref: str, ref_type: str) -> dict:
        if self.spec.site.transport == "http":
            return await self._track_http(ref, ref_type)

        async with self.io_lock:
            for attempt in (1, 2):
                await self._search(ref, ref_type)
                html = await self.session.content()
                data = self.extract(html, ref, ref_type)
                if data:
                    if self._sel["source"] == "auto" and self.cache:
                        self.cache.set("selectors", {"input": self._sel["input"], "submit": self._sel.get("submit")})
                    return data

                if attempt == 1 and self._sel["source"] == "cache":
                    self.cache.delete("selectors")
                    await self.session.goto(self.url)
                    await self._on_landing()
                    await self._resolve_selectors(force=True)
                    continue
                break

            raise TransientError(f"no shipment data extracted from page for {ref}")

    def extract(self, html: str, ref: str, ref_type: str) -> dict:
        """Extract shipment data from page HTML using Scrapling."""
        extractor = ScraplingExtractor(self.spec.scraping, self.spec.block_markers or None)
        return extractor.extract(html, ref)

    async def _wait_ready(self, submit_selector: str | None) -> None:
        """Wait until the page is interactive and submit button is enabled."""
        site = self.spec.site
        wait_ready = getattr(self.session, "wait_ready", None)
        if wait_ready is not None:
            try:
                await wait_ready(submit_selector, timeout_s=10)
            except Exception as exc:
                self.log.warning("wait_ready did not complete: %s", exc)
            return
        await asyncio.sleep(max(site.settle_s or 0, 1.5))

    async def _search(self, ref: str, ref_type: str) -> None:
        s, sel, site = self.session, self._sel, self.spec.site

        if site.ref_type_select:
            await s.select_option(
                site.ref_type_select,
                site.ref_type_map.get(ref_type, ref_type),
            )

        if not await s.wait_for_selector(
            sel["input"],
            timeout_s=site.response_timeout_s,
        ):
            raise TransientError(f"tracking input not available: {sel['input']}")

        await self._wait_ready(sel.get("submit"))

        # Submit via button if available, retrying on transient failures
        if sel.get("submit"):
            for attempt in range(1, SUBMIT_ATTEMPTS + 1):
                try:
                    await s.fill(sel["input"], ref)
                    await self._before_submit(ref)
                    await s.click(sel["submit"])
                    await self._wait_for_result(ref)
                    return
                except (NotFoundError, BlockedError, ManualInterventionRequired):
                    raise
                except Exception as exc:
                    self.log.warning(
                        "submit attempt %d/%d failed for %s: %s",
                        attempt,
                        SUBMIT_ATTEMPTS,
                        ref,
                        exc,
                    )
                if attempt < SUBMIT_ATTEMPTS:
                    await asyncio.sleep(SUBMIT_RETRY_PAUSE_S)

        # Fallback: submit by pressing Enter
        await s.fill(sel["input"], ref)
        await self._before_submit(ref)
        await s.press(sel["input"], "Enter")
        await self._wait_for_result(ref)

    async def _before_submit(self, ref: str) -> None:
        """Hook called after typing reference and before clicking submit/pressing Enter."""

    async def _is_challenge_active(self) -> bool:
        """Hook to detect if an interactive CAPTCHA challenge is active."""
        return False

    async def _wait_for_result(self, ref: str) -> None:
        """Wait until tracking result or error state appears in the DOM."""
        site = self.spec.site
        scraping = self.spec.scraping
        result_selector = site.result_selector or scraping.container_selector or scraping.row_selector
        not_found_selector = site.not_found_selector or scraping.not_found_selector

        deadline = time.monotonic() + site.response_timeout_s
        while time.monotonic() < deadline:
            # 1. Blocked check
            if await self.session.looks_blocked(self.spec.block_markers or None):
                raise BlockedError("carrier site blocked the request")

            # 2. Challenge active check
            if await self._is_challenge_active():
                raise ManualInterventionRequired("CAPTCHA challenge presented (human intervention required)", self.url)

            # 3. Not found check
            if not_found_selector and await self.session.is_visible(not_found_selector):
                raise NotFoundError(ref)

            # 4. Result rendered check
            if result_selector and await self.session.is_visible(result_selector):
                if site.settle_s:
                    await asyncio.sleep(site.settle_s)
                return

            await asyncio.sleep(0.3)

        # Final check after deadline
        if await self.session.looks_blocked(self.spec.block_markers or None):
            raise BlockedError("carrier site blocked the request")
        if not_found_selector and await self.session.is_visible(not_found_selector):
            raise NotFoundError(ref)

        raise TransientError(f"tracking result did not appear within {site.response_timeout_s}s for {ref}")

    async def _track_http(self, ref: str, ref_type: str) -> dict:
        site = self.spec.site
        if not site.http_url_template:
            raise ManualInterventionRequired("transport=http but site.http_url_template is not set")
        url = site.http_url_template.format(ref=ref, ref_type=site.ref_type_map.get(ref_type, ref_type))
        try:
            async with httpx.AsyncClient(timeout=20, headers={"Accept": "text/html,application/xhtml+xml"}) as c:
                r = await c.get(url)
        except httpx.HTTPError as e:
            raise TransientError(str(e)) from e
        if r.status_code in (403, 429):
            raise BlockedError(f"http {r.status_code}")
        if r.status_code == 404:
            raise NotFoundError(ref)
        if r.status_code >= 500:
            raise TransientError(f"http {r.status_code}")
        return self.extract(r.text, ref, ref_type)