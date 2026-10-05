"""Calix x Estes adapter. Open JSON endpoints; transport=browser (intercept the page XHR) or http."""
from __future__ import annotations

import httpx

from app.core.adapter import CarrierAdapter
from app.core.postprocess import get_path
from app.core.resilience import BlockedError, NotFoundError, TransientError


class EstesAdapter(CarrierAdapter):
    @property
    def _http(self) -> bool:
        return self.cfg.get("transport", "browser") == "http"

    async def authenticate(self) -> None:
        if self._http:
            return  # public endpoints, nothing to do
        self.session = await self.browser.open_session(self.session_key)
        await self.session.goto(self.cfg["tracking_page"])
        if await self.session.looks_blocked(self.spec.block_markers or None):
            raise BlockedError("estes: block page on landing")

    async def track(self, ref: str, ref_type: str) -> dict:
        raw = await (self._track_http(ref, ref_type) if self._http else self._track_browser(ref, ref_type))
        nf = self.cfg.get("not_found_if_empty")
        if nf and get_path(raw, nf) is None:
            raise NotFoundError(ref)
        return raw

    async def _track_http(self, ref: str, ref_type: str) -> dict:
        url = self.cfg["http_url_template"].format(ref=ref, ref_type=self.cfg.get("ref_type_map", {}).get(ref_type, ref_type))
        try:
            async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/json"}) as c:
                r = await c.get(url)
        except httpx.HTTPError as e:
            raise TransientError(str(e)) from e
        if r.status_code in (403, 429):
            raise BlockedError(f"estes http {r.status_code}")
        if r.status_code == 404:
            raise NotFoundError(ref)
        if r.status_code >= 500:
            raise TransientError(f"estes http {r.status_code}")
        return r.json()

    async def _track_browser(self, ref: str, ref_type: str) -> dict:
        c = self.cfg
        async with self.io_lock:
            s = self.session
            if c.get("ref_type_select"):
                await s.select_option(c["ref_type_select"], c.get("ref_type_map", {}).get(ref_type, ref_type))
            await s.fill(c["input_selector"], ref)
            got = await s.capture_json(c["response_regex"], lambda: s.click(c["submit_selector"]),
                                       timeout_s=c.get("response_timeout_s", 30))
        for g in got:
            if g.status in (403, 429):
                raise BlockedError(f"estes {g.status}")
        ok = [g for g in got if g.status == 200 and g.json is not None]
        if not ok:
            raise TransientError("estes: no matching JSON response")
        return ok[-1].json
