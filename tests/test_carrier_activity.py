import asyncio
from datetime import datetime, timezone

from app.core.carrier_activity import (
    build_add_carrier_activity,
    pegasus_datetime,
    pegasus_ml_status,
    post_scrape_activities,
)
from app.core.scraper import ScrapeBatchResult
from app.models import CarrierConfig, ShipmentRef, TrackingJob, TrackingResult
from tests.test_batch_scraper import make_spec


def _job(housebill: str, pro: str) -> TrackingJob:
    config = CarrierConfig(
        id=1,
        carrierName="Saia",
        carrierCode="SAI001",
        trackingUrl="https://www.saia.com/track",
        billTo=134034,
        customerName="Calix",
        isActive=True,
        userName=None,
        password=None,
    )
    return TrackingJob(
        carrier=config,
        shipment=ShipmentRef(housebill=housebill, proNumber=pro, vendorId="SAI001"),
        search_by="proNumber",
        search_value=pro,
    )


def test_pegasus_datetime_format():
    assert pegasus_datetime("2026-10-11T07:22:20.874Z") == "2026-10-11T07:22:20Z"
    assert pegasus_datetime(datetime(2026, 10, 11, 7, 22, 20, tzinfo=timezone.utc)) == "2026-10-11T07:22:20Z"


def test_build_add_carrier_activity_shape():
    job = _job("HB123", "771043317403")
    result = TrackingResult(
        reference="771043317403",
        ref_type="PRO",
        carrier_code="SAIA",
        bill_to="134034",
        status="DELIVERED",
        delivery_date="2026-10-01T10:00:00Z",
        events=[{"timestamp": "2026-10-01T09:00:00Z", "description": "Delivered", "status": "Delivered"}],
    )
    start = datetime(2026, 10, 11, 7, 22, 20, tzinfo=timezone.utc)
    end = datetime(2026, 10, 11, 7, 22, 25, tzinfo=timezone.utc)
    payload = build_add_carrier_activity(
        job, result, outcome="found", summary={}, ml_start=start, ml_end=end,
        ml_status=2, ml_log="ok",
    )
    assert payload["housebill"] == "HB123"
    assert payload["mlEndDateTime"] == "2026-10-11T07:22:25Z"
    assert payload["events"][0]["eventDateTime"] == "2026-10-01T09:00:00Z"
    assert payload["proNumber"] == "771043317403"
    assert payload["carrierCode"] == "SAI001"
    assert payload["billTo"] == 134034
    assert payload["mlStatus"] == 2
    assert len(payload["events"]) == 1
    assert payload["events"][0]["description"] == "Delivered"


class RecordingBackend:
    def __init__(self):
        self.payloads: list[dict] = []

    async def add_carrier_activity(self, payload):
        self.payloads.append(payload)


def test_post_scrape_activities_one_per_housebill():
    jobs = [_job("H1", "p1"), _job("H2", "p2")]
    outcome = ScrapeBatchResult(
        requested=["p1", "p2"],
        received={"p1": {"referenceNumbers": {"proNumber": "p1"}, "shipment": {"status": "In Transit"}}},
        outcomes={"p1": "found"},
        summaries={"p1": {}},
        missing=["p2"],
    )
    spec = make_spec("single_batch", batch_size=2)
    spec.site.response_pro_path = "referenceNumbers.proNumber"
    spec.mapping.fields = {"reference": "referenceNumbers.proNumber", "status": "shipment.status"}
    spec.mapping.status_map = {"In Transit": "IN_TRANSIT"}
    backend = RecordingBackend()

    class Settings:
        dry_run = False

    stats = asyncio.run(post_scrape_activities(Settings(), backend, jobs, spec, "run1", outcome))
    assert stats["posted"] == 2
    assert len(backend.payloads) == 2
    by_hb = {p["housebill"]: p for p in backend.payloads}
    assert by_hb["H1"]["mlStatus"] == 2
    assert by_hb["H2"]["mlStatus"] == 3
    assert "missing" in by_hb["H2"]["mlLog"]


def test_post_logs_scrape_capture_and_tms_payload(caplog):
    jobs = [_job("H1", "p1")]
    raw = {"referenceNumbers": {"proNumber": "p1"}, "shipment": {"status": "In Transit"}}
    outcome = ScrapeBatchResult(
        requested=["p1"], received={"p1": raw}, outcomes={"p1": "found"}, summaries={"p1": {}},
    )
    spec = make_spec("single_batch", batch_size=1)
    spec.site.response_pro_path = "referenceNumbers.proNumber"
    spec.mapping.fields = {"reference": "referenceNumbers.proNumber", "status": "shipment.status"}
    spec.mapping.status_map = {"In Transit": "IN_TRANSIT"}

    class Settings:
        dry_run = False

    with caplog.at_level("INFO"):
        asyncio.run(post_scrape_activities(Settings(), RecordingBackend(), jobs, spec, "run1", outcome))
    text = caplog.text
    assert "--- scrape capture housebill=H1 pro=p1 ---" in text
    assert '"proNumber": "p1"' in text
    assert "--- TMS payload (AddCarrierActivity / payload_map)" in text
    assert '"billTo": 134034' in text
    assert '"carrierCode": "SAI001"' in text
    assert '"housebill": "H1"' in text


def test_pegasus_ml_status_maps_outcomes():
    spec = make_spec("single_batch", batch_size=1)
    assert pegasus_ml_status(spec, "ok") == 2
    assert pegasus_ml_status(spec, "not_found") == 4
    assert pegasus_ml_status(spec, "no_response") == 3


def test_bill_to_not_overwritten_by_tracking_result():
    job = _job("HB123", "771043317403")
    result = TrackingResult(
        reference="771043317403",
        ref_type="PRO",
        carrier_code="SAIA",
        bill_to="CALIX",
        status="DELIVERED",
    )
    start = datetime.now(timezone.utc)
    payload = build_add_carrier_activity(
        job, result, outcome="found", summary={}, ml_start=start, ml_end=start,
        ml_status=2, ml_log="ok",
    )
    assert payload["billTo"] == 134034
    assert payload["carrierCode"] == "SAI001"
    assert "billTo" in payload


def test_rejected_api_body_counts_as_failed():
    jobs = [_job("H1", "p1")]
    outcome = ScrapeBatchResult(
        requested=["p1"],
        received={"p1": {"referenceNumbers": {"proNumber": "p1"}, "shipment": {"status": "In Transit"}}},
        outcomes={"p1": "found"},
        summaries={"p1": {}},
    )
    spec = make_spec("single_batch", batch_size=1)
    spec.site.response_pro_path = "referenceNumbers.proNumber"
    spec.mapping.fields = {"reference": "referenceNumbers.proNumber", "status": "shipment.status"}

    class RejectingBackend:
        async def add_carrier_activity(self, payload):
            assert payload["billTo"] == 134034
            return {"ok": True, "status_code": 200, "body": {"isSuccess": False, "message": "rejected"}}

    class Settings:
        dry_run = False

    stats = asyncio.run(post_scrape_activities(Settings(), RejectingBackend(), jobs, spec, "run1", outcome))
    assert stats["posted"] == 0
    assert stats["failed"] == 1


def test_not_found_sets_ml_status():
    job = _job("H9", "p9")
    outcome = ScrapeBatchResult(
        requested=["p9"],
        received={"p9": []},
        outcomes={"p9": "not_found"},
        summaries={"p9": {"error_message": "Not found."}},
    )
    spec = make_spec("single_batch", batch_size=1)
    start = datetime.now(timezone.utc)
    payload = build_add_carrier_activity(
        job, None, outcome="not_found", summary={"error_message": "Not found."},
        ml_start=start, ml_end=start, ml_status=4, ml_log="Not found.", ml_error="Not found.",
    )
    assert payload["mlStatus"] == 4
    assert payload["mlError"] == "Not found."
