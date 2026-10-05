"""Saia = the generic flow + Cloudflare + Google reCAPTCHA handling.

Why this is enough: the page itself does the CSRF token, reCAPTCHA validation,
and the encrypted PRO parameter; we never forge them.
We reuse saved cookies, wait for in-browser challenges, handle the reCAPTCHA checkbox,
and raise ManualInterventionRequired if an image challenge appears."""
from __future__ import annotations

import asyncio
import time

from app.core.adapter import GenericAdapter
from app.core.resilience import ManualInterventionRequired

RECAPTCHA_ANCHOR_FRAME = 'iframe[title="reCAPTCHA"]:visible'
RECAPTCHA_CHECKBOX = "#recaptcha-anchor"


class SaiaAdapter(GenericAdapter):
    async def _on_landing(self) -> None:
        await self._clear_challenge()
        login = self.spec.site.extra.get("login")          # optional: site.extra.login in mapping.yaml
        if login and self.account.user_name and self.account.password:
            s = self.session
            await s.goto(login["url"])
            await self._clear_challenge()
            await s.fill(login["user_selector"], self.account.user_name)
            await s.fill(login["pass_selector"], self.account.password.get_secret_value())
            await s.click(login["submit_selector"])
            if not await s.wait_for_selector(login["logged_in_selector"], 25):
                raise ManualInterventionRequired("saia login did not complete (bad credentials / MFA?)", login["url"])
            await s.goto(self.url)
            await self._clear_challenge()
        await self.session.save_state()

    async def _clear_challenge(self) -> None:
        deadline = time.monotonic() + self.spec.site.challenge_wait_s
        while await self.session.looks_blocked(self.spec.block_markers or None):
            if time.monotonic() > deadline:
                raise ManualInterventionRequired("saia cloudflare challenge unresolved", self.url)
            await asyncio.sleep(min(3, max(0.05, self.spec.site.challenge_wait_s / 10)))

    async def _before_submit(self, ref: str) -> None:
        """Handle Saia Google reCAPTCHA checkbox before clicking submit."""
        await self._handle_recaptcha()

    async def _is_recaptcha_verified(self) -> bool:
        if await self.session.is_visible(
            "#recaptcha-anchor[aria-checked='true']",
            frame=RECAPTCHA_ANCHOR_FRAME,
        ):
            return True
        return bool(await self.session.evaluate("""() => {
            const g = document.querySelector('#g-recaptcha-response-1') || document.querySelector('#g-recaptcha-response');
            return !!(g && g.value && g.value.length > 0);
        }"""))

    async def _is_challenge_active(self) -> bool:
        return bool(await self.session.evaluate("""() => {
            const bframes = Array.from(document.querySelectorAll('iframe[src*="recaptcha/api2/bframe"], iframe[title*="recaptcha challenge"]'));
            for (const bf of bframes) {
                const rect = bf.getBoundingClientRect();
                const style = window.getComputedStyle(bf);
                if (rect.width > 200 && rect.height > 200 && rect.top > 0 && style.visibility === 'visible' && style.display !== 'none') {
                    return true;
                }
            }
            return false;
        }"""))

    async def _handle_recaptcha(self) -> None:
        s = self.session

        # 1. Is a reCAPTCHA on the page at all? Check the main DOM directly, independent of the frame helper.
        deadline = time.monotonic() + 15
        present = False
        while time.monotonic() < deadline:
            present = bool(await s.evaluate("""() => !!document.querySelector(
                'iframe[src*="recaptcha/api2/anchor"], iframe[title="reCAPTCHA"]')"""))
            if present:
                break
            await asyncio.sleep(0.5)
        if not present:
            self.log.info("no reCAPTCHA iframe found on page; skipping")
            return

        if await self._is_recaptcha_verified():
            return

        # 2. Click the checkbox. Never fall through to TRACK if we couldn't.
        if not await self._click_recaptcha_checkbox():
            raise ManualInterventionRequired(
                "saia recaptcha is on the page but the checkbox could not be clicked", self.url)

        # 3. Wait for the result (unchanged logic).
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            if await self._is_recaptcha_verified():
                self.log.info("Saia reCAPTCHA verified successfully")
                await asyncio.sleep(0.3)
                return
            if await self._is_challenge_active():
                raise ManualInterventionRequired(
                    "saia recaptcha challenge presented (human intervention required)", self.url)
        raise ManualInterventionRequired("saia recaptcha verification timed out", self.url)

    async def _click_recaptcha_checkbox(self) -> bool:
        s = self.session
        self.log.info("Clicking Saia reCAPTCHA checkbox")

        # a) existing helper
        try:
            await s.click(RECAPTCHA_CHECKBOX, frame=RECAPTCHA_ANCHOR_FRAME)
            return True
        except Exception as exc:
            self.log.warning("helper click on reCAPTCHA failed: %s", exc)

        page = getattr(s, "page", None)      # adjust if your session names it differently
        if page is None:
            return False

        # b) direct Playwright frame_locator (first matching iframe only, avoids strict-mode errors)
        try:
            frame = page.frame_locator('iframe[title="reCAPTCHA"]').first
            await frame.locator("#recaptcha-anchor").click(timeout=5000)
            return True
        except Exception as exc:
            self.log.warning("frame_locator click failed: %s", exc)

        # c) last resort: real mouse click at the checkbox position inside the iframe
        try:
            box = await page.locator('iframe[title="reCAPTCHA"]').first.bounding_box()
            if box:
                await page.mouse.move(box["x"] + 20, box["y"] + 30, steps=8)
                await page.mouse.click(box["x"] + 29 + 3, box["y"] + 38 + 2)
                return True
        except Exception as exc:
            self.log.warning("coordinate click failed: %s", exc)
        return False