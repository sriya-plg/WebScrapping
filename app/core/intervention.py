"""Best-effort human-intervention alerting with process-local deduplication (no database)."""
from __future__ import annotations

import logging
import time

import httpx

from app.logging_setup import log
from app.settings import AlertSettings

logger = logging.getLogger(__name__)


class InterventionQueue:
    """Despite the legacy name, this is intentionally not a persistent queue."""

    def __init__(self, alerts: AlertSettings):
        self.alerts = alerts
        self._last_alert: dict[str, float] = {}

    async def raise_(self, run_id: str, client: str, carrier: str, bill_to: str, refs: list[str], reason: str,
                     url: str | None = None) -> None:
        log(logger, logging.ERROR, "manual intervention required", run_id=run_id, client=client,
            carrier=carrier, bill_to=bill_to, refs=len(refs), reason=reason, url=url)
        key = f"{client}:{carrier}:{reason}"
        now = time.time()
        if now - self._last_alert.get(key, 0) < self.alerts.dedupe_hours * 3600:
            return
        self._last_alert[key] = now
        if not self.alerts.webhook_url:
            return
        text = (f"Tracker needs human intervention: {client}/{carrier} ({reason}). "
                f"{len(refs)} refs for billTo {bill_to}. Tracking URL: {url or 'see logs'}")
        try:
            async with httpx.AsyncClient(timeout=10) as client_http:
                response = await client_http.post(self.alerts.webhook_url, json={"text": text})
                response.raise_for_status()
        except Exception as exc:
            log(logger, logging.WARNING, "intervention alert failed", error=type(exc).__name__)
