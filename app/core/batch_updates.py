"""Per-PRO backend update hook for the batch scrape path (map/post wiring comes later)."""
from __future__ import annotations

import logging
from typing import Any

from app.core.mapper import get_path
from app.core.scraper import ScrapeBatchResult, classify_carrier_raw
from app.logging_setup import log
from app.models import CarrierConfig
from app.settings import CarrierSpec

logger = logging.getLogger(__name__)


def build_update_item(
    pro: str,
    raw: Any,
    outcome: str,
    summary: dict[str, Any],
    run_id: str,
    spec: CarrierSpec,
    bill_to: int | str,
) -> dict[str, Any]:
    """Shape passed to backend post_tracking once mapping is implemented."""
    return {
        "runId": run_id,
        "billTo": bill_to,
        "carrierCode": spec.code,
        "reference": pro,
        "referenceType": "PRO",
        "scrapeOutcome": outcome,
        "carrierErrorCode": summary.get("error_code"),
        "carrierErrorMessage": summary.get("error_message"),
        "carrierRaw": raw,
        "tracking": None,
    }


async def enqueue_tracking_updates(
    settings,
    backend,
    carrier: CarrierConfig,
    spec: CarrierSpec,
    run_id: str,
    outcome: ScrapeBatchResult,
) -> None:
    """Log and optionally POST one update per PRO that returned carrier JSON."""
    items: list[dict[str, Any]] = []
    for pro in outcome.requested:
        raw = outcome.received.get(pro)
        if raw is None:
            continue
        outcome_type = outcome.outcomes.get(pro, "captured")
        summary = outcome.summaries.get(pro, {})
        item = build_update_item(pro, raw, outcome_type, summary, run_id, spec, carrier.bill_to)
        items.append(item)
        log(
            logger,
            logging.INFO,
            "tracking update stub",
            pro=pro,
            outcome=outcome_type,
            error_code=summary.get("error_code"),
            error_message=summary.get("error_message"),
            data_count=summary.get("data_count"),
        )

    if not items:
        log(logger, logging.INFO, "tracking update stub skipped", reason="no carrier JSON captured")
        return

    if settings.dry_run:
        log(logger, logging.INFO, "dry-run: would post tracking updates", count=len(items))
        return

    # Real Pegasus update endpoint TBD; keep a single call site for batch path posts.
    log(logger, logging.INFO, "tracking update post pending implementation", count=len(items))
    _ = backend
