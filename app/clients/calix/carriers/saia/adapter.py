"""Saia: Cloudflare + CSRF + encrypted PRO param. We never forge any of that: a real (undetected) browser
performs the page flow and we read the JSON it receives. The 'manual step' becomes:
  1. reuse persisted cookies (cf_clearance) -> usually no challenge at all
  2. if challenged, wait for the in-browser check to self-resolve (headed browser often passes)
  3. if still blocked -> ManualInterventionRequired (queue + alert); a human runs `assist saia` ONCE
     to refresh the cookie profile; the next run resumes automatically."""
from __future__ import annotations

import asyncio
import time

from app.core.adapter import CarrierAdapter
from app.core.postprocess import get_path
from app.core.resilience import BlockedError, ManualInterventionRequired, NotFoundError, TransientError


class SaiaAdapter(CarrierAdapter):
    async def authenticate(self) -> None:
        c = self.cfg
        self.session = await self.browser.open_session(self.session_key)
        await self.session.goto(c["tracking_page"])
        await self._clear_challenge()
        if c.get("login_required") and self.account.user_name and self.account.password:
            await self.session.goto(c["login_url"])
            await self._clear_challenge()
            await self.session.fill(c["login_user_selector"], self.account.user_name)
            await self.session.fill(c["login_pass_selector"], self.account.password.get_secret_value())
            await self.session.click(c["login_submit_selector"])
            if not await self.session.wait_for_selector(c["logged_in_selector"], 25):
                raise ManualInterventionRequired("saia login did not complete (bad creds / MFA?)", c["login_url"])
            await self.session.goto(c["tracking_page"])
        await self.session.save_state()

    async def _clear_challenge(self) -> None:
        wait_s = self.cfg.get("challenge_wait_s", 60)
        deadline = time.monotonic() + wait_s
        markers = self.spec.block_markers or None
        while await self.session.looks_blocked(markers):
            if time.monotonic() > deadline:
                raise ManualInterventionRequired("saia cloudflare challenge unresolved", self.cfg["tracking_page"])
            await asyncio.sleep(3)

    async def track(self, ref: str, ref_type: str) -> dict:
        c = self.cfg
        async with self.io_lock:
            s = self.session
            if c.get("ref_type_select"):
                await s.select_option(c["ref_type_select"], c.get("ref_type_map", {}).get(ref_type, ref_type))
            await s.fill(c["input_selector"], ref)   # the page encrypts the PRO + attaches CSRF itself
            got = await s.capture_json(c["response_regex"], lambda: s.click(c["submit_selector"]),
                                       timeout_s=c.get("response_timeout_s", 40))
            if await s.looks_blocked(self.spec.block_markers or None):
                raise BlockedError("saia: challenge page after submit")
        if any(g.status in (403, 429) for g in got):
            raise BlockedError("saia 403/429")
        ok = [g for g in got if g.status == 200 and g.json is not None]
        if not ok:
            raise TransientError("saia: no matching JSON response")
        raw = ok[-1].json
        if c.get("not_found_if_empty") and get_path(raw, c["not_found_if_empty"]) is None:
            raise NotFoundError(ref)
        await s.save_state()
        return raw
