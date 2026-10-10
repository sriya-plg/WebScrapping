"""Typed planning helper for API intake; scraper execution remains in the existing runner."""
from __future__ import annotations

import logging

from app.models import CarrierConfig, TrackingJob

logger = logging.getLogger(__name__)


async def build_jobs(backend) -> dict[str, dict[str, list[TrackingJob]]]:
    """Fetch active carrier shipments and group jobs by customer then carrier.

    Shipment failures are isolated to their carrier group. The production Runner
    uses the same BackendClient methods through AccountJob to retain existing
    scraper pacing, retries, and result processing.
    """
    configs = await backend.get_carrier_configs()
    grouped: dict[str, dict[str, list[TrackingJob]]] = {}
    groups: dict[tuple[int, str], CarrierConfig] = {}
    for config in configs:
        if config.is_active:
            groups.setdefault((config.bill_to, config.carrier_code), config)
    for (bill_to, carrier_code), config in groups.items():
        try:
            shipments = await backend.get_shipments(bill_to, carrier_code)
        except Exception as exc:
            logger.warning("shipment intake failed", extra={"fields": {
                "bill_to": bill_to, "carrier_code": carrier_code, "error": type(exc).__name__}})
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
        if skipped:
            logger.info("shipment rows skipped", extra={"fields": {"bill_to": bill_to,
                        "carrier_code": carrier_code, "skipped": skipped}})
    return grouped
