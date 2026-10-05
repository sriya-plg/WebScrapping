"""'Needs manual intervention' queue + deduped alert instead of silent failure."""
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

    async def raise_(self, run_id: str, client: str, carrier: str, bill_to: str, refs: list[str], reason: str,
                     url: str | None = None) -> None:
        try:
            added = self.store.enqueue(run_id, client, carrier, bill_to, refs, reason)
            log(logger, logging.ERROR, "manual intervention required", client=client, carrier=carrier,
                bill_to=bill_to, refs=len(refs), newly_queued=added, reason=reason, url=url)
            key = f"alert:{client}:{carrier}:{reason}"
            if time.time() - float(self.store.kv_get(key) or 0) < self.alerts.dedupe_hours * 3600:
                return
            self.store.kv_set(key, str(time.time()))
            if self.alerts.webhook_url:
                text = (f":warning: Tracker needs a human: {client}/{carrier} ({reason}). {len(refs)} refs for "
                        f"billTo {bill_to} waiting. Fix: python -m app.main assist {carrier} --client {client}")
                async with httpx.AsyncClient(timeout=10) as c:
                    await c.post(self.alerts.webhook_url, json={"text": text})
        except Exception as e:   # alerting/queueing must never break a run
            log(logger, logging.WARNING, "intervention/alert failed", error=type(e).__name__)
