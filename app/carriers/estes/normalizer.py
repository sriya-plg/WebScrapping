"""The only Estes-specific code in the whole app: turning their JSON
response shape into the shared TrackingResult schema."""

from app.core.exceptions import ScraperNotFoundError
from app.core.models import TrackingEvent, TrackingResult


def normalize(payload: dict, reference: str) -> TrackingResult:
    error = payload.get("error") or {}
    if error.get("code"):
        raise RuntimeError(f"Estes API error: {error.get('message')}")

    data = payload.get("data") or []
    if not data:
        raise ScraperNotFoundError(f"No shipment found for PRO {reference}")

    shipment = data[0]
    status = shipment.get("status", {}) or {}

    history = []
    for leg in shipment.get("movementHistory", []) or []:
        for entry in leg.get("statusHistory", []) or []:
            history.append(
                TrackingEvent(
                    date=entry.get("referenceDate", ""),
                    status=entry.get("conciseStatus", ""),
                    description=entry.get("expandedStatus", ""),
                )
            )

    return TrackingResult(
        carrier="estes",
        reference=reference,
        status=status.get("conciseStatus"),
        status_detail=status.get("expandedStatus"),
        pickup_date=shipment.get("pickupDate"),
        delivery_date=shipment.get("deliveryDate"),
        pieces=shipment.get("piecesCount"),
        weight_lbs=shipment.get("totalWeight"),
        shipper=(shipment.get("shipperParty") or {}).get("address", {}).get("city"),
        consignee=(shipment.get("consigneeParty") or {}).get("address", {}).get("city"),
        history=history,
        raw=shipment,
    )
