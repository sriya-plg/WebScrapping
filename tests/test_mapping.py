"""Carrier JSON -> TrackingResult -> AddCarrierActivity, driven by mapping.yaml / client.yaml."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

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
    result = normalize(SAIA, FULL, "771043317403", "PRO", ACCT)
    assert result.status == "DELIVERED" and result.delivered
    assert result.service_level == "LTL"
    assert result.pod_name == "J SMITH"
    assert result.pod_datetime.startswith("2026-10-11")
    assert len(result.events) == 3
    last = result.events[-1]
    assert last.code == "DL"
    assert last.description == "Delivered"
    assert last.timezone == "CST"
    assert last.lat == 29.76
    assert last.long == -95.37


def test_full_details_become_add_carrier_activity_payload():
    result = normalize(SAIA, FULL, "771043317403", "PRO", ACCT)
    payload = build_add_carrier_activity(
        _job(), result, outcome="found", summary={},
        ml_start=START, ml_end=END, ml_status=2, ml_log="ok", spec=SAIA,
    )
    assert payload["housebill"] == "HB123"
    assert payload["proNumber"] == "771043317403"
    assert payload["carrierCode"] == "SAI001"
    assert payload["billTo"] == 134034
    assert payload["serviceLevel"] == "LTL"
    assert payload["podName"] == "J SMITH"
    assert payload["podDateTime"] == "2026-10-11T14:30:00Z"
    assert payload["mlStatus"] == 2
    assert payload["mlStartDateTime"] == "2026-10-11T07:22:20Z"
    assert len(payload["events"]) == 3
    ev = payload["events"][-1]
    assert ev["code"] == "DL"
    assert ev["description"] == "Delivered"
    assert ev["eventDateTime"] == "2026-10-11T14:30:00Z"
    assert ev["timezone"] == "CST"
    assert ev["lat"] == 29.76
    assert ev["long"] == -95.37


def test_yaml_payload_map_override_is_respected():
    spec = SAIA.model_copy(deep=True)
    spec.activity.payload_map["serviceLevel"] = "=STD"
    result = normalize(spec, FULL, "771043317403", "PRO", ACCT)
    payload = build_add_carrier_activity(
        _job(), result, outcome="found", summary={},
        ml_start=START, ml_end=END, ml_status=2, ml_log="ok", spec=spec,
    )
    assert payload["serviceLevel"] == "STD"


def test_extra_mapping_field_flows_into_payload_without_model_change():
    spec = SAIA.model_copy(deep=True)
    spec.mapping.fields["driver"] = "delivery.signedBy"
    spec.activity.payload_map["reference"] = "driver"
    result = normalize(spec, FULL, "771043317403", "PRO", ACCT)
    assert result.driver == "J SMITH"
    payload = build_add_carrier_activity(
        _job(), result, outcome="found", summary={},
        ml_start=START, ml_end=END, ml_status=2, ml_log="ok", spec=spec,
    )
    assert payload["reference"] == "J SMITH"


def test_scraper_prefers_find_full_details_over_find_by_pro(tmp_path):
    pro = "771043317403"
    entered = "77104331740"
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
    assert raw["shipment"]["status"] == "Delivered"


def test_estes_fixture_still_maps_events():
    raw = json.loads((ROOT / "tests/fixtures/estes_response.json").read_text())
    acct = CarrierAccount(id=1, carrierName="Estes", carrierCode="ESTES", billTo="CALIX")
    result = normalize(ESTES, raw, "1234567890", "PRO", acct)
    assert result.status == "DELIVERED"
    assert result.events[0].description == "Delivered"
