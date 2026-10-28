"""Typed planning helper for API intake; scraper execution remains in the existing runner."""
from __future__ import annotations

import logging

from app.logging_setup import log
from app.models import CarrierConfig, TrackingJob

logger = logging.getLogger(__name__)


async def build_jobs(backend) -> dict[str, dict[str, list[TrackingJob]]]:
    """Fetch active carrier shipments and group jobs by customer then carrier.

    Shipment failures are isolated to their carrier group. The production Runner
    uses the same BackendClient methods through AccountJob to retain existing
    scraper pacing, retries, and result processing.
    """
    configs = await backend.get_carrier_configs()
    active = sum(1 for c in configs if c.is_active)
    log(logger, logging.INFO, "carrier configurations loaded", total=len(configs), active=active)
    grouped: dict[str, dict[str, list[TrackingJob]]] = {}
    groups: dict[tuple[int, str], CarrierConfig] = {}
    for config in configs:
        if config.is_active:
            groups.setdefault((config.bill_to, config.carrier_code), config)
    for (bill_to, carrier_code), config in groups.items():
        try:
            shipments = await backend.get_shipments(bill_to, carrier_code)
        except Exception as exc:
            logger.exception("shipment intake failed | bill_to=%s carrier_code=%s error=%s",
                             bill_to, carrier_code, exc)
            continue
        search_by = config.search_by or "proNumber"
        seen: set[str] = set()
        customer = config.customer_name or str(bill_to)
        carrier_jobs = grouped.setdefault(customer, {}).setdefault(carrier_code, [])
        skipped = 0
        for shipment in shipments:
            value = str(getattr(shipment, {"proNumber": "pro_number", "housebill": "housebill",
                                           "vendorId": "vendor_id"}.get(search_by, "pro_number"), "") or "").strip()
            key = value.casefold()
            if not value or key in seen:
                skipped += 1
                continue
            seen.add(key)
            carrier_jobs.append(TrackingJob(carrier=config, shipment=shipment,
                                            search_by=search_by, search_value=value))
        log(logger, logging.INFO, "pending shipments loaded", bill_to=bill_to,
            carrier_code=carrier_code, job_count=len(carrier_jobs), skipped=skipped)
    total_jobs = sum(len(jobs) for carriers in grouped.values() for jobs in carriers.values())
    log(logger, logging.INFO, "intake complete", carrier_groups=len(groups), tracking_jobs=total_jobs)
    return grouped
