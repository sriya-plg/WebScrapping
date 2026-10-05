"""'Needs manual intervention' queue + alert (deduped) instead of silent failure."""
from __future__ import annotations

import logging
import time

import httpx

from app.core.state import StateStore
from app.logging_setup import log
from app.settings import AlertSettings

logger = logging.getLogger(__name__)


class InterventionQueue:
    def __init__(self, store: StateStore, alerts: AlertSettings):
        self.store, self.alerts = store, alerts

    async def raise_(self, run_id: str, carrier: str, bill_to: str, refs: list[str], reason: str,
                     url: str | None = None, client: str = "") -> None:
        self.store.enqueue(run_id, carrier, bill_to, refs, reason)
        log(logger, logging.ERROR, "manual intervention required", client=client, carrier=carrier, bill_to=bill_to,
            refs=len(refs), reason=reason, url=url)
        key = f"alert:{client}:{carrier}:{reason}"
        last = float(self.store.kv_get(key) or 0)
        if time.time() - last < self.alerts.dedupe_hours * 3600:
            return
        self.store.kv_set(key, str(time.time()))
        if self.alerts.webhook_url:
            text = (f":warning: Tracker needs a human: {client}/{carrier} ({reason}). {len(refs)} refs for billTo "
                    f"{bill_to} queued. Run: python -m app.main assist {carrier} --client {client}")
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    await c.post(self.alerts.webhook_url, json={"text": text})
            except Exception as e:  # alerting must never break the run
                log(logger, logging.WARNING, "alert webhook failed", error=type(e).__name__)
