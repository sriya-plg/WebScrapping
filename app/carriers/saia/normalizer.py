"""The only Saia-specific code: turning their JSON response shape into
the shared TrackingResult schema.

Note: sample response used fullDetails=false -- no event history is
included. If history is needed, either flip that param (if the live
form exposes it) or capture the request fired when clicking into a
specific shipment's detail view instead."""

from app.core.exceptions import ScraperNotFoundError
from app.core.models import TrackingResult


def normalize(payload, reference: str) -> TrackingResult:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        raise RuntimeError(f"Saia API error(s): {errors}")

    # Saia's response is a flat list, not a {data: [...]} wrapper
    results = payload if isinstance(payload, list) else payload.get("results", [])
    if not results:
        raise ScraperNotFoundError(f"No shipment found for PRO {reference}")

    shipment = results[0]
    delivery = shipment.get("delivery", {}) or {}
    shipment_info = shipment.get("shipment", {}) or {}

    return TrackingResult(
        carrier="saia",
        reference=reference,
        status=shipment_info.get("status"),
        pickup_date=shipment_info.get("pickupDate"),
        delivery_date=delivery.get("deliveredDate"),
        shipper=(shipment.get("shipper") or {}).get("companyName"),
        consignee=(shipment.get("consignee") or {}).get("companyName"),
        history=[],
        raw=shipment,
    )
