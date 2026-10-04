"""The only Forward Air-specific code: turning their JSON response shape
into the shared TrackingResult schema.

Note: this endpoint can return multiple matches for one reference number
(confirmed via testing) -- we take the most recent by RecordDate."""

from app.core.exceptions import ScraperNotFoundError
from app.core.models import TrackingEvent, TrackingResult


def normalize(payload: dict, reference: str) -> TrackingResult:
    errors = payload.get("errors") or []
    if errors:
        raise RuntimeError(f"Forward Air API error(s): {errors}")

    results = payload.get("results") or []
    if not results:
        raise ScraperNotFoundError(f"No shipment found for reference {reference}")

    shipment = max(results, key=lambda r: r.get("RecordDate", ""))

    history = []
    for entry in reversed(shipment.get("Statuses", []) or []):
        history.append(
            TrackingEvent(
                date=entry.get("RecordDate", ""),
                status=entry.get("statusCode", ""),
                description=entry.get("StatusDescription", ""),
                location=entry.get("AirportCode"),
            )
        )

    return TrackingResult(
        carrier="forward_air",
        reference=reference,
        status=shipment.get("CurrentStatusDescription"),
        delivery_date=shipment.get("PodDate"),
        pieces=shipment.get("Pieces"),
        weight_lbs=shipment.get("Weight"),
        shipper=shipment.get("originName") or shipment.get("Origin"),
        consignee=shipment.get("destinationName") or shipment.get("Destination"),
        history=history,
        raw=shipment,
    )
