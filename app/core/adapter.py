"""Adapter contract + GenericAdapter (the default for every carrier).

GenericAdapter works for most sites with ZERO per-carrier code:
  1. open the tracking URL (from the backend record)           2. find the search box (auto, cached, or pinned)
  3. type the reference, click the button (fallback: Enter)    4. capture the JSON the page receives and pick
     the response that contains our reference                   5. hand the raw JSON to the mapper
Override ONE small method in a subclass only when a site needs it (see Saia: _on_landing)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.browser.base import BrowserProvider, BrowserSession, CapturedResponse
from app.core import mapper
from app.core.resilience import BlockedError, ManualInterventionRequired, NotFoundError, TransientError
from app.models import CarrierAccount, TrackingResult
from app.settings import CarrierSpec

# How many times we re-fill + re-click the submit button before falling back to Enter.
# Single-page apps (Angular, e.g. Estes) ignore clicks made before the form is bound/valid,
# so a click that produces no tracking response is retried rather than abandoned.
SUBMIT_ATTEMPTS = 3
SUBMIT_ATTEMPT_TIMEOUT_S = 12
SUBMIT_RETRY_PAUSE_S = 2

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
    def __init__(self, spec: CarrierSpec, account: CarrierAccount, browser: BrowserProvider | None,
                 cache: Cache | None = None):
        self.spec, self.account, self.browser, self.cache = spec, account, browser, cache
        self.session: BrowserSession | None = None
        self.io_lock = asyncio.Lock()      # one page per session -> serialize browser interaction
        self.log = logging.getLogger(f"adapter.{spec.client}.{spec.code}")

    async def authenticate(self) -> None:
        """Open session / log in / clear challenge. Raise BlockedError or ManualInterventionRequired."""

    @abstractmethod
    async def track(self, ref: str, ref_type: str) -> dict:
        """RAW carrier JSON for one reference. Raise NotFoundError / TransientError / BlockedError."""

    def normalize(self, raw: dict, ref: str, ref_type: str) -> TrackingResult:
        return mapper.normalize(self.spec, raw, ref, ref_type, self.account)

    async def close(self) -> None:
        if self.session:
            try:
                await self.session.close()
            finally:
                self.session = None


def _site(host: str) -> str:
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:])


def _contains_ref(obj: Any, key: str) -> bool:
    if isinstance(obj, dict):
        return any(_contains_ref(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_ref(v, key) for v in obj)
    return obj is not None and key in re.sub(r"\W", "", str(obj)).lower()


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
                return False                       # no banner: nothing to do
        except Exception:
            return False
        for sel in sels:
            try:
                if await s.wait_for_selector(sel, timeout_s=0.5):
                    await s.click(sel)
                    self.log.info("dismissed cookie banner via %s", sel)
                    await asyncio.sleep(1)         # let the banner animate away
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
        # A committed navigation can precede SPA hydration. Give the tracking form
        # time to render instead of treating an initially empty DOM as final.
        deadline = asyncio.get_running_loop().time() + self.spec.site.response_timeout_s
        found = None
        while not found or not found.get("input"):
            found = await self.session.discover_search_box()
            if found and found.get("input"):
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.5, remaining))
        if not found or not found.get("input"):
            raise ManualInterventionRequired("could not find a search box; set site.input_selector in mapping.yaml",
                                             self.url)
        self._sel = {"input": found["input"], "submit": s.submit_selector or found.get("submit"), "source": "auto"}

    def _should_capture_network(self) -> bool:
        if not self.spec.site.capture_network:
            return False
        strat = getattr(self.spec, "scraping", {}).get("strategy")
        if strat == "scrapling":
            return False
        return True

    def extract(self, html: str, ref: str, ref_type: str) -> dict:
        """Extract shipment data from page HTML without network responses."""
        return {}

    # ------------------------------------------------------------------ tracking
    async def track(self, ref: str, ref_type: str) -> dict:
        if self.spec.site.transport == "http":
            return await self._track_http(ref, ref_type)
        got: list[CapturedResponse] = []
        async with self.io_lock:
            for attempt in (1, 2):
                got = await self._search(ref, ref_type)
                if not self._should_capture_network():
                    html = await self.session.content() if hasattr(self.session, "content") else ""
                    data = self.extract(html, ref, ref_type)
                    if data:
                        return data
                    if attempt == 2:
                        raise TransientError(f"no shipment data extracted from page for {ref}")
                    continue
                hit = self._pick(got, ref)
                if hit:
                    if self._sel["source"] == "auto" and self.cache:     # remember what worked
                        self.cache.set("selectors", {"input": self._sel["input"], "submit": self._sel.get("submit")})
                    return hit.json if isinstance(hit.json, dict) else {"data": hit.json}
                if attempt == 1 and self._sel["source"] == "cache":      # cached selectors went stale
                    self.cache.delete("selectors")
                    await self.session.goto(self.url)
                    await self._on_landing()
                    await self._resolve_selectors(force=True)
                    continue
                break
            await self._raise_for_miss(got, ref)

    async def _wait_ready(self, submit_selector: str | None) -> None:
        """Wait until the page is interactive: network quiet (best effort) and submit button enabled.

        Uses BrowserSession.wait_ready() when the session provides it; otherwise falls back to a short
        settle delay so the adapter still works with sessions that haven't implemented it yet."""
        site = self.spec.site
        wait_ready = getattr(self.session, "wait_ready", None)
        if wait_ready is not None:
            try:
                await wait_ready(submit_selector, timeout_s=10)
            except Exception as exc:     # never fail the run just because readiness couldn't be proven
                self.log.warning("wait_ready did not complete: %s", exc)
            return
        await asyncio.sleep(max(site.settle_s or 0, 2))

    async def _search(self, ref: str, ref_type: str) -> list[CapturedResponse]:
        s, sel, site = self.session, self._sel, self.spec.site

        if site.ref_type_select:
            await s.select_option(
                site.ref_type_select,
                site.ref_type_map.get(ref_type, ref_type),
            )

        # Wait until the tracking input is actually available.
        if not await s.wait_for_selector(
            sel["input"],
            timeout_s=site.response_timeout_s,
        ):
            raise TransientError(
                f"tracking input not available: {sel['input']}"
            )

        # The input existing in the DOM is not enough on single-page apps: wait until the page is
        # bootstrapped and the submit button is enabled, otherwise the click is silently ignored.
        await self._wait_ready(sel.get("submit"))

        capture_enabled = self._should_capture_network()
        if not capture_enabled:
            await s.fill(sel["input"], ref)
            await self._before_submit(ref)
            if sel.get("submit"):
                await s.click(sel["submit"])
            else:
                await s.press(sel["input"], "Enter")
            return []

        kw = dict(
            settle_s=site.settle_s,
            until=lambda rs: self._pick(rs, ref) is not None or self._has_not_found(rs),
            enabled=True,
        )

        # Keep every response seen across attempts so _raise_for_miss can diagnose properly
        # (previously the Enter fallback overwrote everything with an empty capture).
        all_got: list[CapturedResponse] = []

        # --------------------------------------------------------------
        # Preferred path: click the Search/Track button, retrying on a miss.
        # More reliable than Enter for sites such as Estes, where Enter
        # does not submit the tracking form. The click can fail fast when
        # the Angular form isn't ready, so we re-fill and click again.
        # --------------------------------------------------------------
        # Preferred path: click the Search/Track button, retrying on a miss.
        # More reliable than Enter for sites such as Estes, where Enter
        # does not submit the tracking form. The click can fail fast when
        # the Angular form isn't ready, so we re-fill and click again.
        # --------------------------------------------------------------
        if sel.get("submit"):
            for attempt in range(1, SUBMIT_ATTEMPTS + 1):
                try:
                    await s.fill(sel["input"], ref)      # re-fill: the app may have reset the form
                    await self._before_submit(ref)
                    got = await s.capture_all(
                        lambda: s.click(sel["submit"]),
                        timeout_s=min(site.response_timeout_s, SUBMIT_ATTEMPT_TIMEOUT_S),
                        **kw,
                    )
                    all_got += got
                    if self._pick(got, ref) is not None:
                        return all_got
                    if self._has_not_found(got):
                        raise NotFoundError(ref)
                    self.log.warning("submit attempt %d/%d for %s produced no matching response",
                                     attempt, SUBMIT_ATTEMPTS, ref)
                except NotFoundError:
                    raise
                except Exception as exc:
                    self.log.warning("submit attempt %d/%d failed for %s: %s",
                                     attempt, SUBMIT_ATTEMPTS, ref, exc)
                if attempt < SUBMIT_ATTEMPTS:
                    await asyncio.sleep(SUBMIT_RETRY_PAUSE_S)

        # --------------------------------------------------------------
        # Fallback: press Enter.
        # Some carriers submit the search box with Enter but do not have
        # a usable submit button.
        # --------------------------------------------------------------
        await s.fill(sel["input"], ref)
        await self._before_submit(ref)
        got = await s.capture_all(
            lambda: s.press(sel["input"], "Enter"),
            timeout_s=site.response_timeout_s,
            **kw,
        )
        if self._has_not_found(got):
            raise NotFoundError(ref)
        return all_got + got

    async def _before_submit(self, ref: str) -> None:
        """Hook called after typing reference and before clicking submit/pressing Enter.
        Allows carrier adapters to handle intermediate steps (e.g. CAPTCHAs, consent prompts)
        without duplicating _search()."""

    def _pick(self, got: list[CapturedResponse], ref: str) -> CapturedResponse | None:
        site = self.spec.site
        rx = re.compile(site.response_regex) if site.response_regex else None
        key = re.sub(r"\W", "", ref).lower()
        cands = [g for g in got if g.status == 200 and isinstance(g.json, (dict, list)) and (not rx or rx.search(g.url))]
        if site.match_ref:
            cands = [g for g in cands if _contains_ref(g.json, key)]
        return max(cands, key=lambda g: len(json.dumps(g.json)), default=None)   # richest match

    def _has_not_found(self, got: list[CapturedResponse]) -> bool:
        base = _site(urlparse(self.url or "").hostname or "")
        return any(g.status == 404 and g.json is not None
                   and _site(urlparse(g.url).hostname or "") == base for g in got)

    async def _raise_for_miss(self, got: list[CapturedResponse], ref: str) -> None:
        if any(g.status in (403, 429) for g in got) or await self.session.looks_blocked(self.spec.block_markers or None):
            raise BlockedError("carrier site blocked the request")
        base = _site(urlparse(self.url or "").hostname or "")
        if self._has_not_found(got) or any(
                g.status == 200 and g.json is not None and _site(urlparse(g.url).hostname or "") == base
                for g in got):
            raise NotFoundError(ref)     # site answered with JSON, but none of it is about this reference
        raise TransientError("no JSON response captured (site may render HTML only: it needs a custom adapter)")

    async def _track_http(self, ref: str, ref_type: str) -> dict:
        site = self.spec.site
        if not site.http_url_template:
            raise ManualInterventionRequired("transport=http but site.http_url_template is not set")
        url = site.http_url_template.format(ref=ref, ref_type=site.ref_type_map.get(ref_type, ref_type))
        try:
            async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/json"}) as c:
                r = await c.get(url)
        except httpx.HTTPError as e:
            raise TransientError(str(e)) from e
        if r.status_code in (403, 429):
            raise BlockedError(f"http {r.status_code}")
        if r.status_code == 404:
            raise NotFoundError(ref)
        if r.status_code >= 500:
            raise TransientError(f"http {r.status_code}")
        data = r.json()
        return data if isinstance(data, dict) else {"data": data}
