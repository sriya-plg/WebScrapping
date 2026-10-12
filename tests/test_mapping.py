"""Carrier JSON -> TrackingResult -> AddCarrierActivity, driven by mapping.yaml / client.yaml."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.carrier_activity import build_add_carrier_activity
from app.core.mapper import normalize
from app.models import CarrierAccount, CarrierConfig, ShipmentRef, TrackingJob
from app.settings import load_resolver
from tests.test_batch_scraper import MockProvider, MockSession, make_jobs
from app.browser.base import CapturedResponse
from app.core.scraper import BatchScraper

ROOT = Path(__file__).resolve().parents[1]
RES = load_resolver(ROOT / "app/clients")
SAIA = RES.resolve("CALIX", "SAIA")
ESTES = RES.resolve("CALIX", "ESTES")
ACCT = CarrierAccount(id=1, carrierName="Saia", carrierCode="SAIA", billTo="134034")
FULL = json.loads((ROOT / "tests/fixtures/saia_find_full_details.json").read_text())
SUMMARY = json.loads((ROOT / "tests/fixtures/saia_sample.json").read_text())
START = datetime(2026, 10, 11, 7, 22, 20, tzinfo=timezone.utc)
END = datetime(2026, 10, 11, 7, 22, 25, tzinfo=timezone.utc)


def _job(housebill="HB123", pro="771043317403") -> TrackingJob:
    config = CarrierConfig(
        id=1, carrierName="Saia", carrierCode="SAI001",
        trackingUrl="https://www.saia.com/track", billTo=134034,
        customerName="Calix", isActive=True,
    )
    return TrackingJob(
        carrier=config,
        shipment=ShipmentRef(housebill=housebill, proNumber=pro, vendorId="SAI001"),
        search_by="proNumber", search_value=pro,
    )


def test_saia_spec_prefers_full_details_api():
    assert SAIA is not None
    assert "find-full-detail" in (SAIA.site.response_url_pattern or "")
    assert "find-by-pro-number" in (SAIA.site.response_url_pattern or "")
    assert SAIA.site.response_url_prefer == "find-full-detail"
    assert "service_level" in SAIA.mapping.fields
    assert SAIA.mapping.events and "history" in SAIA.mapping.events["path"]
    assert "podName" in SAIA.activity.payload_map
    assert "eventDateTime" in SAIA.activity.events_map


def test_saia_find_by_pro_maps_summary_without_events():
    raw = SUMMARY[0]
    result = normalize(SAIA, raw, "771043317403", "PRO", ACCT)
    assert result.status == "IN_TRANSIT"
    assert result.reference == "771043317403"
    assert result.events == []


def test_saia_find_full_details_maps_complete_fields():
    result = normalize(SAIA, FULL, "77137570910", "PRO", ACCT)
    assert result.status == "DELIVERED" and result.delivered
    assert result.service_level == "PPD"
    assert result.pod_name == "Received by-Shawn"
    assert result.pod_datetime.startswith("2026-09-15T12:13")
    assert result.origin == "CALIX"
    assert result.destination == "ALLO COMMUNICATIONS"
    assert len(result.events) == 3
    first = result.events[0]
    assert first.code == "Delivered"
    assert first.description == "Delivered"
    assert first.location == "YUMA, AZ"
    assert first.timestamp == "2026-09-15T12:13:00"


def test_full_details_become_add_carrier_activity_payload():
    result = normalize(SAIA, FULL, "77137570910", "PRO", ACCT)
    payload = build_add_carrier_activity(
        _job("HB123", "77137570910"), result, outcome="found", summary={},
        ml_start=START, ml_end=END, ml_status=2, ml_log="ok", spec=SAIA,
    )
    assert payload["housebill"] == "HB123"
    assert payload["proNumber"] == "77137570910"
    assert payload["carrierCode"] == "SAI001"
    assert payload["billTo"] == 134034
    assert payload["serviceLevel"] == "PPD"
    assert payload["podName"] == "Received by-Shawn"
    assert payload["podDateTime"] == "2026-09-15T12:13:00Z"
    assert payload["mlStatus"] == 2
    assert payload["mlStartDateTime"] == "2026-10-11T07:22:20Z"
    assert len(payload["events"]) == 3
    ev = payload["events"][0]
    assert ev["code"] == "Delivered"
    assert ev["description"] == "Delivered"
    assert ev["eventDateTime"] == "2026-09-15T12:13:00Z"
    assert ev["timezone"] == "UTC"
    assert ev["lat"] == 0
    assert ev["long"] == 0


def test_yaml_payload_map_override_is_respected():
    spec = SAIA.model_copy(deep=True)
    spec.activity.payload_map["serviceLevel"] = "=STD"
    result = normalize(spec, FULL, "77137570910", "PRO", ACCT)
    payload = build_add_carrier_activity(
        _job("HB123", "77137570910"), result, outcome="found", summary={},
        ml_start=START, ml_end=END, ml_status=2, ml_log="ok", spec=spec,
    )
    assert payload["serviceLevel"] == "STD"


def test_extra_mapping_field_flows_into_payload_without_model_change():
    spec = SAIA.model_copy(deep=True)
    spec.mapping.fields["driver"] = "deliveryInformation.signature"
    spec.activity.payload_map["reference"] = "driver"
    result = normalize(spec, FULL, "77137570910", "PRO", ACCT)
    assert result.driver == "Received by-Shawn"
    payload = build_add_carrier_activity(
        _job("HB123", "77137570910"), result, outcome="found", summary={},
        ml_start=START, ml_end=END, ml_status=2, ml_log="ok", spec=spec,
    )
    assert payload["reference"] == "Received by-Shawn"


def test_scraper_prefers_find_full_details_over_find_by_pro(tmp_path):
    pro = "77137570910"
    entered = "77137570910"
    summary = [{"referenceNumbers": {"proNumber": pro}, "shipment": {"status": "In Transit"}}]
    session = MockSession({
        entered: [
            CapturedResponse("https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, summary),
            CapturedResponse("https://www.saia.com/api/v1/tracking/find-full-details", 200, FULL),
        ]
    })
    spec = SAIA.model_copy(deep=True)
    spec.site.action_min_delay_s = 0
    spec.site.action_max_delay_s = 0
    spec.site.idle_window_s = 0.01
    spec.site.pre_submit_delay_s = 0
    spec.site.typing_delay_ms = 0
    spec.site.inter_pro_delay_s = 0
    spec.site.login = None
    spec.site.extra = {
        **(spec.site.extra or {}),
        "retry_missing_individually": False,
        "landing_settle_s": 0,
        "return_to_tracking_after_submit": False,
    }
    result = BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch
    import asyncio
    outcome = asyncio.run(result(make_jobs([pro]), spec))
    assert outcome.missing == []
    raw = outcome.received[pro]
    assert raw.get("history")
    assert raw["shipmentInformation"]["status"] == "Delivered"


def test_estes_fixture_still_maps_events():
    raw = json.loads((ROOT / "tests/fixtures/estes_response.json").read_text())
    acct = CarrierAccount(id=1, carrierName="Estes", carrierCode="ESTES", billTo="CALIX")
    result = normalize(ESTES, raw, "1234567890", "PRO", acct)
    assert result.status == "DELIVERED"
    assert result.events[0].description == "Delivered"


def _estes_job(housebill="23671345", pro="184-8684463") -> TrackingJob:
    config = CarrierConfig(
        id=1, carrierName="Estes", carrierCode="ESTE03",
        trackingUrl="https://www.estes-express.com/myestes/shipment-tracking/",
        billTo=134034, customerName="Calix", isActive=True,
    )
    return TrackingJob(
        carrier=config,
        shipment=ShipmentRef(housebill=housebill, proNumber=pro, vendorId="ESTE03"),
        search_by="proNumber", search_value=pro,
    )


def test_estes_movement_history_maps_to_add_carrier_activity():
    """Each Estes statusHistory scan becomes an event: code, expanded description, time, coordinates."""
    raw = {
        "data": [{
            "pro": "1848718241",
            "deliveryDate": "2026-10-07",
            "deliveryTime": "13:01:59-05:00",
            "receivedBy": "Seth",
            "driverInfo": {"name": " Andrew"},
            "status": {"conciseStatus": "Delivered", "expandedStatus": "Delivery Completed - OK"},
            "originTerminal": {"geoCoordinates": ["-97.3410885", "32.837842"]},
            "destinationTerminal": {"geoCoordinates": ["-96.713927", "43.59406"]},
            "movementHistory": [
                {
                    "id": "Pickup",
                    "description": "Actual Pickup",
                    "transportEventTypeCode": "Actual Pickup",
                    "statusHistory": [{
                        "expandedStatus": "Pickup Location Serviced",
                        "referenceDate": "2026-10-02",
                        "referenceTime": "19:33:51-05:00",
                    }],
                },
                {
                    "id": "164",
                    "description": "Planned Waypoint",
                    "transportEventTypeCode": "Planned Waypoint",
                    "location": {
                        "name": "Kansas City",
                        "geoCoordinates": ["-94.670554", "39.089552"],
                    },
                    "statusHistory": [
                        {
                            "expandedStatus": "En route to Estes facility at Kansas City, KS (164)",
                            "referenceDate": "2026-10-06",
                            "referenceTime": "02:44:48-05:00",
                        },
                        {
                            "expandedStatus": "Departed Estes facility at Kansas City, KS (164)",
                            "referenceDate": "2026-10-06",
                            "referenceTime": "21:37:45-05:00",
                        },
                    ],
                },
                {
                    "id": "Delivery",
                    "description": "Actual Delivery",
                    "transportEventTypeCode": "Delivery",
                    "statusHistory": [
                        {
                            "expandedStatus": "Out for Delivery",
                            "referenceDate": "2026-10-07",
                            "referenceTime": "10:22:44-05:00",
                        },
                        {
                            "expandedStatus": "Delivery Completed - OK",
                            "referenceDate": "2026-10-07",
                            "referenceTime": "13:01:59-05:00",
                        },
                    ],
                },
            ],
        }],
    }
    acct = CarrierAccount(id=1, carrierName="Estes", carrierCode="ESTE03", billTo="134034")
    result = normalize(ESTES, raw, "184-8684463", "PRO", acct)
    assert result.reference == "1848718241"
    assert result.pod_name == "Seth"
    assert result.pod_datetime.startswith("2026-10-07T13:01:59")
    assert [e.description for e in result.events] == [
        "Pickup Location Serviced",
        "En route to Estes facility at Kansas City, KS (164)",
        "Departed Estes facility at Kansas City, KS (164)",
        "Out for Delivery",
        "Delivery Completed - OK",
    ]
    assert result.events[0].code == "Actual Pickup"
    assert result.events[0].timezone == "-05:00"
    assert result.events[0].lat == pytest.approx(32.837842)
    assert result.events[0].long == pytest.approx(-97.3410885)
    assert result.events[1].code == "Planned Waypoint"
    assert result.events[1].timestamp.startswith("2026-10-06T02:44:48")
    assert result.events[2].timestamp.startswith("2026-10-06T21:37:45")
    assert result.events[1].lat == pytest.approx(39.089552)
    assert result.events[1].long == pytest.approx(-94.670554)
    assert result.events[-1].code == "Delivery"
    assert result.events[-1].lat == pytest.approx(43.59406)
    assert result.events[-1].long == pytest.approx(-96.713927)

    payload = build_add_carrier_activity(
        _estes_job(), result, outcome="found", summary={},
        ml_start=START, ml_end=END, ml_status=2, ml_log="ok", spec=ESTES,
    )
    assert payload["proNumber"] == "184-8684463"
    assert payload["carrierCode"] == "ESTE03"
    assert payload["reference"] == "1848718241"
    assert payload["podName"] == "Seth"
    assert payload["podDateTime"] == "2026-10-07T18:01:59Z"
    assert [ev["code"] for ev in payload["events"]] == [
        "Actual Pickup", "Planned Waypoint", "Planned Waypoint", "Delivery", "Delivery",
    ]
    assert all(len(ev["code"]) <= 50 for ev in payload["events"])
    assert payload["events"][1]["description"] == "En route to Estes facility at Kansas City, KS (164)"
    assert payload["events"][1]["eventDateTime"] == "2026-10-06T07:44:48Z"
    assert payload["events"][1]["timezone"] == "-05:00"
    assert payload["events"][2]["description"] == "Departed Estes facility at Kansas City, KS (164)"
    assert payload["events"][2]["eventDateTime"] == "2026-10-07T02:37:45Z"
    assert payload["events"][-1]["description"] == "Delivery Completed - OK"
    assert payload["events"][-1]["eventDateTime"] == "2026-10-07T18:01:59Z"
