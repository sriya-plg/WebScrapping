"""Batch scrape path -> Pegasus AddCarrierActivity (one POST per housebill)."""
from __future__ import annotations

from app.core.carrier_activity import post_scrape_activities
from app.core.scraper import ScrapeBatchResult
from app.models import CarrierConfig, TrackingJob
from app.settings import CarrierSpec


async def enqueue_tracking_updates(
    settings,
    backend,
    carrier: CarrierConfig,
    spec: CarrierSpec,
    run_id: str,
    outcome: ScrapeBatchResult,
    jobs: list[TrackingJob] | None = None,
) -> dict[str, int]:
    if not jobs:
        return {"posted": 0, "skipped": 0, "failed": 0}
    return await post_scrape_activities(settings, backend, jobs, spec, run_id, outcome)
