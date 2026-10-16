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
FORWARDAIR = RES.resolve("CALIX", "FORWARDAIR")
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
    assert SAIA.site.response_url_prefer is None
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
    assert "timezone" not in ev
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
        "Actual Pickup",
        "En route to Estes facility at Kansas City, KS (164)",
        "Departed Estes facility at Kansas City, KS (164)",
        "Planned Waypoint",
        "Out for Delivery",
        "Delivery Completed - OK",
        "Actual Delivery",
    ]
    assert result.events[0].code == "Pickup Location Serviced"
    assert result.events[0].timezone == "-05:00"
    assert result.events[0].lat == pytest.approx(32.837842)
    assert result.events[0].long == pytest.approx(-97.3410885)
    assert result.events[2].timestamp == "2026-10-06T02:44:48-05:00"
    assert result.events[3].timestamp == "2026-10-06T21:37:45-05:00"
    assert result.events[2].lat == pytest.approx(39.089552)
    assert result.events[2].long == pytest.approx(-94.670554)
    assert result.events[4].code == "Planned Waypoint"
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
    # POD times retain the existing Pegasus local clock-time behavior.
    assert payload["podDateTime"] == "2026-10-07T13:01:59Z"
    assert [ev["code"] for ev in payload["events"]] == [
        "Pickup Location Serviced", "Actual Pickup",
        "En route to Estes facility at Kansas City, KS (164",
        "Departed Estes facility at Kansas City, KS (164)", "Planned Waypoint",
        "Out for Delivery", "Delivery Completed - OK", "Delivery",
    ]
    assert all(len(ev["code"]) <= 50 for ev in payload["events"])
    assert payload["events"][2]["description"].startswith("En route to Estes facility")
    assert payload["events"][2]["eventDateTime"] == "2026-10-06T02:44:48-05:00"
    assert payload["events"][2]["timezone"] == "-05:00"
    assert payload["events"][4]["description"] == "Planned Waypoint"
    assert payload["events"][3]["eventDateTime"] == "2026-10-06T21:37:45-05:00"
    assert payload["events"][-1]["description"] == "Actual Delivery"
    assert payload["events"][-1]["eventDateTime"] == "2026-10-07T13:01:59-05:00"


def test_estes_status_history_adds_movement_events_and_preserves_offsets():
    raw = {
        "data": [{
            "pro": "1848683493",
            "deliveryDate": "2026-09-09",
            "deliveryTime": "09:50:59-06:00",
            "movementHistory": [
                {
                    "id": "225", "description": "Planned Waypoint",
                    "transportEventTypeCode": "Planned Waypoint",
                    "location": {
                        "address": {"city": "Salt Lake City", "state": "UT"},
                        "geoCoordinates": ["-111.967766", "40.742635"],
                    },
                    "statusHistory": [
                        {"conciseStatus": "In Transit", "expandedStatus": "Arrived at facility",
                         "referenceDate": "2026-09-04", "referenceTime": "13:46:52-06:00"},
                        {"conciseStatus": "In Transit", "expandedStatus": "Arrived at facility",
                         "referenceDate": "2026-09-04", "referenceTime": "13:46:52-06:00"},
                        {"conciseStatus": "In Transit", "expandedStatus": "Departed facility",
                         "referenceDate": "2026-09-09", "referenceTime": "01:21:50-06:00"},
                    ],
                },
                {
                    "id": "Delivery", "description": "Actual Delivery",
                    "transportEventTypeCode": "Delivery",
                    "statusHistory": [
                        {"conciseStatus": "Delivered", "expandedStatus": "Delivery Completed - OK",
                         "referenceDate": "2026-09-09", "referenceTime": "09:50:59-06:00"},
                    ],
                },
            ],
        }],
    }
    account = CarrierAccount(id=1, carrierName="Estes", carrierCode="ESTE03", billTo="134034")
    result = normalize(ESTES, raw, "1848683493", "PRO", account)
    # The identical status row is removed; each movement contributes one parent event.
    assert len(result.events) == 5
    assert result.events[0].code == "In Transit"
    assert result.events[0].description == "Arrived at facility"
    assert result.events[0].city == "Salt Lake City"
    assert result.events[0].state == "UT"
    assert result.events[0].timestamp == "2026-09-04T13:46:52-06:00"
    assert result.events[0].timezone == "-06:00"
    assert result.events[0].lat == pytest.approx(40.742635)
    assert result.events[0].long == pytest.approx(-111.967766)
    assert result.events[2].code == "Planned Waypoint"
    assert result.events[2].description == "Planned Waypoint"
    assert result.events[2].timestamp == "2026-09-09T01:21:50-06:00"
    assert result.events[3].city is None
    assert result.events[3].lat is None

    payload = build_add_carrier_activity(
        _estes_job(pro="1848683493"), result, outcome="found", summary={},
        ml_start=START, ml_end=END, ml_status=2, ml_log="ok", spec=ESTES,
    )
    assert payload["events"][0]["eventDateTime"] == "2026-09-04T13:46:52-06:00"
    assert payload["events"][0]["timezone"] == "-06:00"
    assert payload["events"][0]["city"] == "Salt Lake City"
    assert payload["events"][0]["state"] == "UT"
    assert payload["events"][3]["lat"] == 0
    assert payload["events"][3]["long"] == 0


def _forwardair_job(housebill="23600001", pro="96765557") -> TrackingJob:
    config = CarrierConfig(
        id=1, carrierName="Forward Air", carrierCode="FORW-LTL",
        trackingUrl="https://www.forwardair.com/tracking",
        billTo=134034, customerName="Calix", isActive=True,
    )
    return TrackingJob(
        carrier=config,
        shipment=ShipmentRef(housebill=housebill, proNumber=pro, vendorId="FORW-LTL"),
        search_by="proNumber", search_value=pro,
    )


def test_forwardair_sample_maps_pod_and_status_rows():
    """PodSignedBy, PodDate, statusCode, RecordDate, StatusDescription, TimeZoneCode."""
    raw = {
        "CurrentStatusDescription": "Invoiced",
        "PodSignedBy": "Jamie Chisholm",
        "AirbillNumber": "96765557",
        "PodDate": "07/31/2026 10:55",
        "Origin": "DFW",
        "Destination": "MEM",
        "Pieces": 5,
        "Weight": 2127,
        "Statuses": [
            {
                "statusCode": "INVC",
                "RecordDate": "07/14/2026 11:25",
                "StatusDescription": "Invoiced",
                "AirportCode": "GCY",
                "TimeZoneCode": "ET",
            },
            {
                "statusCode": "DLV",
                "RecordDate": "07/31/2026 10:55",
                "StatusDescription": "Delivered",
                "AirportCode": "MEM",
                "TimeZoneCode": "CT",
            },
            {
                "statusCode": "PUP",
                "RecordDate": "07/17/2026 23:13",
                "StatusDescription": "Picked Up",
                "AirportCode": "DFW",
                "TimeZoneCode": "CT",
            },
        ],
    }
    acct = CarrierAccount(id=1, carrierName="Forward Air", carrierCode="FORWARDAIR", billTo="134034")
    result = normalize(FORWARDAIR, raw, "96765557", "PRO", acct)
    assert result.reference == "96765557"
    assert result.pod_name == "Jamie Chisholm"
    assert result.pod_datetime == "2026-07-31T10:55:00"
    assert [(e.code, e.description, e.timezone) for e in result.events] == [
        ("INVC", "Invoiced", "ET"),
        ("DLV", "Delivered", "CT"),
        ("PUP", "Picked Up", "CT"),
    ]

    payload = build_add_carrier_activity(
        _forwardair_job(), result, outcome="found", summary={},
        ml_start=START, ml_end=END, ml_status=2, ml_log="ok", spec=FORWARDAIR,
    )
    assert payload["housebill"] == "23600001"
    assert payload["proNumber"] == "96765557"
    assert payload["reference"] == "96765557"
    assert payload["podName"] == "Jamie Chisholm"
    assert payload["podDateTime"] == "2026-07-31T10:55:00"
    assert payload["events"][0]["code"] == "INVC"
    assert payload["events"][0]["description"] == "Invoiced"
    assert payload["events"][0]["timezone"] == "ET"
    assert payload["events"][0]["eventDateTime"] == "2026-07-14T11:25:00"
    assert payload["events"][1]["code"] == "DLV"
    assert payload["events"][1]["eventDateTime"] == "2026-07-31T10:55:00"
    assert payload["events"][1]["timezone"] == "CT"
    assert payload["events"][2]["eventDateTime"] == "2026-07-17T23:13:00"
    assert payload["events"][2]["timezone"] == "CT"


def test_forwardair_maps_without_system_timezone_database(monkeypatch):
    """Windows venvs without tzdata raised ZoneInfoNotFoundError and dropped every event."""
    import app.core.mapper as mapper

    def _missing(name):
        raise mapper.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(mapper, "ZoneInfo", _missing)
    assert mapper.apply_zone_abbrev("2026-07-31T10:55:00", "CT") == "2026-07-31T10:55:00-05:00"
    assert mapper.apply_zone_abbrev("2026-08-03T10:51:00", "ET") == "2026-08-03T10:51:00-04:00"
    assert mapper.apply_zone_abbrev("2026-01-15T09:00:00", "CT") == "2026-01-15T09:00:00-06:00"
    assert mapper.apply_zone_abbrev("2026-03-08T01:59:00", "ET") == "2026-03-08T01:59:00-05:00"
    assert mapper.apply_zone_abbrev("2026-03-08T02:00:00", "ET") == "2026-03-08T02:00:00-04:00"
    assert mapper.apply_zone_abbrev("2026-11-01T01:59:00", "PT") == "2026-11-01T01:59:00-07:00"
    assert mapper.apply_zone_abbrev("2026-11-01T02:00:00", "PT") == "2026-11-01T02:00:00-08:00"


def test_time_offset_stripped_without_conversion():
    """Times like '15:18:00-05:00' must not be converted to UTC, but sent directly as 15:18:00."""
    from app.core.mapper import strip_time_offset, get_path, compose_value

    # Unit tests for strip_time_offset helper
    assert strip_time_offset("15:18:00-05:00") == "15:18:00"
    assert strip_time_offset("13:01:59-05:00") == "13:01:59"
    assert strip_time_offset("15:18:00+05:00") == "15:18:00"
    assert strip_time_offset("15:18:00-0500") == "15:18:00"
    assert strip_time_offset("15:18:00.123-05:00") == "15:18:00.123"
    assert strip_time_offset("15:18-05:00") == "15:18"
    assert strip_time_offset("15:18:00") == "15:18:00"
    assert strip_time_offset(None) is None

    # Extraction via get_path
    obj = {"deliveryTime": "15:18:00-05:00", "deliveryDate": "2026-10-07"}
    assert get_path(obj, "deliveryTime") == "15:18:00"

    # Composition via compose_value
    assert compose_value(obj, "{deliveryDate}T{deliveryTime}") == "2026-10-07T15:18:00"

    # pegasus_datetime preserves 15:18:00 without UTC conversion
    from app.core.carrier_activity import pegasus_datetime
    assert pegasus_datetime("15:18:00-05:00", fallback=datetime(2026, 10, 7, tzinfo=timezone.utc)) == "2026-10-07T15:18:00Z"
    assert pegasus_datetime("15:18:00", fallback=datetime(2026, 10, 7, tzinfo=timezone.utc)) == "2026-10-07T15:18:00Z"
    assert pegasus_datetime("2026-10-07T15:18:00") == "2026-10-07T15:18:00Z"

