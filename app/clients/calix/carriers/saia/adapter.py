"""Saia = the generic flow + Cloudflare handling. Only `_on_landing` is overridden.

Why this is enough: the page itself does the CSRF token and the encrypted PRO parameter; we never forge them.
We reuse saved cookies (usually no challenge), give an in-browser challenge time to resolve, and otherwise
raise ManualInterventionRequired (queue + alert). A human then runs `assist saia --client calix` once."""
import asyncio
import time

from app.core.adapter import GenericAdapter
from app.core.resilience import ManualInterventionRequired


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
