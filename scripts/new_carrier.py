"""python scripts/new_carrier.py calix forwardair [--adapter]  ->  carriers/forwardair/mapping.yaml (+ adapter.py)"""
import pathlib, sys
client, folder = sys.argv[1].lower(), sys.argv[2].lower()
d = pathlib.Path(__file__).resolve().parents[1] / "app/clients" / client / "carriers" / folder
d.mkdir(parents=True, exist_ok=True)
(d / "__init__.py").touch()
(d / "mapping.yaml").write_text(f"""# {client} x {folder}
# Probe first: python scripts/probe.py {client} {folder} <ref> --headed
# Then uncomment and fill dotted JSON paths. `|` = first non-empty fallback.
# AddCarrierActivity keys live in ../../client.yaml  (activity.payload_map / events_map).

aliases: []

# site:
#   tracking_url: https://example.com/track
#   input_selector: "#pro"
#   submit_selector: "button:has-text('Track')"
#   response_url_pattern: tracking/api          # regex; can match more than one URL
#   response_url_prefer: full-details           # optional richer API when two fire
#   expected_response_shape: per_pro            # per_pro | single_batch
#   response_pro_path: data[].proNumber

# mapping:
#   auto: true
#   fields:
#     reference: proNumber
#     status: shipment.status
#     pickup_date: shipment.pickupDate
#     delivery_date: delivery.deliveredDate
#     eta: delivery.expectedDate
#     origin: shipper.city
#     destination: consignee.city
#     pieces: shipment.pieces
#     weight: shipment.weight
#     service_level: shipment.serviceLevel
#     pod_name: delivery.signedBy
#     pod_datetime: delivery.deliveredDate
#   events:
#     path: history
#     fields:
#       timestamp: date
#       description: description
#       location: city
#       status: status
#       code: statusCode
#       timezone: timezone
#       lat: latitude
#       long: longitude
#   status_map:
#     Delivered: DELIVERED
#     "In Transit": IN_TRANSIT
""")
if "--adapter" in sys.argv:
    (d / "adapter.py").write_text('''"""Custom behaviour for this carrier ONLY. Override the smallest hook you need."""
from app.core.adapter import GenericAdapter


class CustomAdapter(GenericAdapter):
    async def _on_landing(self) -> None:       # e.g. dismiss a banner, clear a challenge, log in
        await super()._on_landing()
''')
print(f"created {d}" + ("" if "--adapter" in sys.argv else "  (no adapter needed unless the site is unusual)"))
