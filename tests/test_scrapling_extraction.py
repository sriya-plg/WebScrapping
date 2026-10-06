"""Tests specifically for the Scrapling page extraction path.

Verifies:
1. Successful Saia result extraction.
2. PRO number extraction.
3. Status extraction.
4. Pickup date extraction.
5. Delivery date extraction.
6. Shipper/consignee extraction.
7. Missing optional fields.
8. Shipment-not-found page.
9. Unexpected/malformed HTML.
10. Result timeout.
11. Normalization of Scrapling output into canonical TrackingResult.
"""
from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from app.core.adapter import GenericAdapter
from app.core.extractor import ScraplingExtractor, parse_date_value
from app.core.resilience import BlockedError, NotFoundError, TransientError
from app.models import CarrierAccount, TrackingResult
from app.settings import CarrierSpec, ScrapingConfig, load_resolver

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
RESOLVER = load_resolver(ROOT_DIR / "app/clients")
SAIA_SPEC = RESOLVER.resolve("CALIX", "SAIA")
SAIA_ACCT = CarrierAccount(id=1, carrierName="Saia", carrierCode="SAIA", billTo="CALIX")

ESTES_SPEC = RESOLVER.resolve("CALIX", "ESTES")
ESTES_ACCT = CarrierAccount(id=2, carrierName="Estes Express", carrierCode="ESTES", billTo="CALIX")

FORWARDAIR_SPEC = RESOLVER.resolve("CALIX", "FORWARDAIR")
FORWARDAIR_ACCT = CarrierAccount(id=3, carrierName="Forward Air", carrierCode="FORWARDAIR", billTo="CALIX")


@pytest.fixture
def delivered_html() -> str:
    return (FIXTURES_DIR / "saia_delivered.html").read_text(encoding="utf-8")


@pytest.fixture
def not_found_html() -> str:
    return (FIXTURES_DIR / "saia_not_found.html").read_text(encoding="utf-8")


@pytest.fixture
def in_transit_html() -> str:
    return (FIXTURES_DIR / "saia_in_transit.html").read_text(encoding="utf-8")


@pytest.fixture
def saia_extractor() -> ScraplingExtractor:
    return ScraplingExtractor(SAIA_SPEC.scraping, SAIA_SPEC.block_markers)


@pytest.fixture
def estes_html() -> str:
    return (FIXTURES_DIR / "estes_result.html").read_text(encoding="utf-8")


@pytest.fixture
def estes_extractor() -> ScraplingExtractor:
    return ScraplingExtractor(ESTES_SPEC.scraping, ESTES_SPEC.block_markers)


@pytest.fixture
def forwardair_html() -> str:
    return (FIXTURES_DIR / "forwardair_result.html").read_text(encoding="utf-8")


@pytest.fixture
def forwardair_extractor() -> ScraplingExtractor:
    return ScraplingExtractor(FORWARDAIR_SPEC.scraping, FORWARDAIR_SPEC.block_markers)


# 1. Successful Saia result extraction
def test_successful_saia_result_extraction(delivered_html, saia_extractor):
    data = saia_extractor.extract(delivered_html, "77133675090")
    assert isinstance(data, dict)
    assert data["reference"] == "77133675090"
    assert data["status"] == "Delivered"


# 2. PRO number extraction
def test_pro_number_extraction(delivered_html, saia_extractor):
    data = saia_extractor.extract(delivered_html, "77133675090")
    assert data["reference"] == "77133675090"


# 3. Status extraction
def test_status_extraction(delivered_html, in_transit_html, saia_extractor):
    data_delivered = saia_extractor.extract(delivered_html, "77133675090")
    assert data_delivered["status"] == "Delivered"

    data_transit = saia_extractor.extract(in_transit_html, "12345678901")
    assert data_transit["status"] == "In Transit"


# 4. Pickup date extraction
def test_pickup_date_extraction(delivered_html, in_transit_html, saia_extractor):
    data_delivered = saia_extractor.extract(delivered_html, "77133675090")
    assert data_delivered["pickup_date"] == "2026-07-10"

    data_transit = saia_extractor.extract(in_transit_html, "12345678901")
    assert data_transit["pickup_date"] == "2026-07-15"


# 5. Delivery date extraction
def test_delivery_date_extraction(delivered_html, in_transit_html, saia_extractor):
    data_delivered = saia_extractor.extract(delivered_html, "77133675090")
    assert data_delivered["delivery_date"] == "2026-07-20"

    # In transit shipment has not been delivered yet
    data_transit = saia_extractor.extract(in_transit_html, "12345678901")
    assert data_transit.get("delivery_date") is None


# 6. Shipper/consignee extraction
def test_shipper_consignee_extraction(delivered_html, in_transit_html, saia_extractor):
    data_delivered = saia_extractor.extract(delivered_html, "77133675090")
    assert data_delivered["consignee"] == "Bam Broadband"
    assert data_delivered["destination"] == "Bam Broadband"

    data_transit = saia_extractor.extract(in_transit_html, "12345678901")
    assert data_transit["consignee"] == "Acme Logistics"
    assert data_transit["destination"] == "Acme Logistics"


# 7. Missing optional fields
def test_missing_optional_fields(delivered_html, in_transit_html, saia_extractor):
    # Delivered shipment: ETA is "-" in table -> parsed as None
    data_delivered = saia_extractor.extract(delivered_html, "77133675090")
    assert data_delivered.get("eta") is None
    assert data_delivered.get("delivery_window") == "12:29 PM - 12:35 PM"

    # In transit shipment: delivery date and window are "-" -> parsed as None, ETA is present
    data_transit = saia_extractor.extract(in_transit_html, "12345678901")
    assert data_transit.get("delivery_date") is None
    assert data_transit.get("delivery_window") is None
    assert data_transit.get("eta") == "2026-07-22"


# 8. Shipment-not-found page
def test_shipment_not_found_page(not_found_html, saia_extractor):
    with pytest.raises(NotFoundError) as exc_info:
        saia_extractor.extract(not_found_html, "99999999999")
    assert "99999999999" in str(exc_info.value)


# 9. Unexpected/malformed HTML
def test_unexpected_malformed_html(saia_extractor):
    # Empty string
    with pytest.raises(TransientError) as exc_empty:
        saia_extractor.extract("", "77133675090")
    assert "empty HTML" in str(exc_empty.value)

    # HTML without any tracking containers
    random_html = "<html><body><div>Welcome to homepage</div></body></html>"
    with pytest.raises(TransientError) as exc_missing:
        saia_extractor.extract(random_html, "77133675090")
    assert "tracking result not found" in str(exc_missing.value)


# 10. Result timeout
async def test_result_timeout():
    class DummySession:
        async def looks_blocked(self, markers=None):
            return False

        async def is_visible(self, selector, frame=None):
            return False  # neither result nor not-found ever appears

    spec = SAIA_SPEC.model_copy()
    spec.site = spec.site.model_copy(update={"response_timeout_s": 0.5})
    adapter = GenericAdapter(spec, SAIA_ACCT, None)
    adapter.session = DummySession()

    with pytest.raises(TransientError) as exc_timeout:
        await adapter._wait_for_result("77133675090")
    assert "did not appear within" in str(exc_timeout.value)


# 11. Normalization of Scrapling output
def test_normalization_of_scrapling_output(delivered_html, in_transit_html, saia_extractor):
    adapter = GenericAdapter(SAIA_SPEC, SAIA_ACCT, None)

    # 11a. Delivered shipment normalization
    raw_delivered = saia_extractor.extract(delivered_html, "77133675090")
    result_delivered = adapter.normalize(raw_delivered, "77133675090", "PRO")

    assert isinstance(result_delivered, TrackingResult)
    assert result_delivered.reference == "77133675090"
    assert result_delivered.ref_type == "PRO"
    assert result_delivered.carrier_code == "SAIA"
    assert result_delivered.bill_to == "CALIX"
    assert result_delivered.status == "DELIVERED"
    assert result_delivered.raw_status == "Delivered"
    assert result_delivered.delivered is True
    assert result_delivered.pickup_date == "2026-07-10"
    assert result_delivered.delivery_date == "2026-07-20"
    assert result_delivered.destination == "Bam Broadband"
    assert result_delivered.raw == raw_delivered

    # 11b. In-transit shipment normalization
    raw_transit = saia_extractor.extract(in_transit_html, "12345678901")
    result_transit = adapter.normalize(raw_transit, "12345678901", "PRO")

    assert result_transit.reference == "12345678901"
    assert result_transit.status == "IN_TRANSIT"
    assert result_transit.raw_status == "In Transit"
    assert result_transit.delivered is False
    assert result_transit.pickup_date == "2026-07-15"
    assert result_transit.delivery_date is None
    assert result_transit.eta == "2026-07-22"
    assert result_transit.destination == "Acme Logistics"


# 12. Blocked page detection
def test_blocked_page_detection(saia_extractor):
    blocked_html = "<html><head><title>Attention Required! | Cloudflare</title></head><body>Verify you are human</body></html>"
    with pytest.raises(BlockedError) as exc_blocked:
        saia_extractor.extract(blocked_html, "77133675090")
    assert "security check" in str(exc_blocked.value)


# 13. Date helper unit test
def test_parse_date_value():
    assert parse_date_value("7/10/26") == "2026-07-10"
    assert parse_date_value("07/10/2026") == "2026-07-10"
    assert parse_date_value("2026-07-10") == "2026-07-10"
    assert parse_date_value("-") is None
    assert parse_date_value("N/A") is None
    assert parse_date_value("") is None


# 14. Successful Estes extraction
def test_estes_result_extraction(estes_html, estes_extractor):
    data = estes_extractor.extract(estes_html, "1234567890")
    assert isinstance(data, dict)
    assert data["reference"] == "1234567890"
    assert data["status"] == "Picked Up"
    assert data["pickup_date"] == "2020-05-19"
    assert data["delivery_date"] == "2020-05-21"
    assert data["origin"] == "BELPRE, OH 45714 US"
    assert data["destination"] == "OAK BROOK, IL US"
    assert data["consignee"] == "OAK BROOK, IL US"
    assert data["shipper"] == "BELPRE, OH 45714 US"
    assert data["pieces"] == "1"
    assert data["weight"] == "5"


# 15. Normalization of Estes output into canonical TrackingResult
def test_estes_normalization(estes_html, estes_extractor):
    adapter = GenericAdapter(ESTES_SPEC, ESTES_ACCT, None)
    data = estes_extractor.extract(estes_html, "1234567890")
    res = adapter.normalize(data, "1234567890", "PRO")

    assert isinstance(res, TrackingResult)
    assert res.reference == "1234567890"
    assert res.ref_type == "PRO"
    assert res.carrier_code == "ESTES"
    assert res.bill_to == "CALIX"
    assert res.status == "PICKED_UP"
    assert res.raw_status == "Picked Up"
    assert res.delivered is False
    assert res.pickup_date == "2020-05-19"
    assert res.delivery_date == "2020-05-21"
    assert res.origin == "BELPRE, OH 45714 US"
    assert res.destination == "OAK BROOK, IL US"
    assert res.pieces == 1
    assert res.weight == 5.0
    assert res.shipper == "BELPRE, OH 45714 US"
    assert res.consignee == "OAK BROOK, IL US"


# 16. Estes not found error
def test_estes_not_found(estes_extractor):
    nf_html = "<html><body><table id='tblData'></table><div class='no-records'>No records found for tracking number 9999999999</div></body></html>"
    with pytest.raises(NotFoundError) as exc_info:
        estes_extractor.extract(nf_html, "9999999999")
    assert "9999999999" in str(exc_info.value)


# 17. Successful Forward Air extraction
def test_forwardair_result_extraction(forwardair_html, forwardair_extractor):
    data = forwardair_extractor.extract(forwardair_html, "97028918")
    assert isinstance(data, dict)
    assert data["reference"] == "97028918"
    assert data["status"] == "On-time"
    assert data["origin"] == "ORD"
    assert data["destination"] == "ONT"
    assert data["amount"] == "9 items x 1120 lbs"
    assert data["pieces"] == 9
    assert data["weight"] == 1120.0
    assert data["current_location"] == "Invoiced at GCY"


# 18. Normalization of Forward Air output into canonical TrackingResult
def test_forwardair_normalization(forwardair_html, forwardair_extractor):
    adapter = GenericAdapter(FORWARDAIR_SPEC, FORWARDAIR_ACCT, None)
    data = forwardair_extractor.extract(forwardair_html, "97028918")
    res = adapter.normalize(data, "97028918", "PRO")

    assert isinstance(res, TrackingResult)
    assert res.reference == "97028918"
    assert res.ref_type == "PRO"
    assert res.carrier_code == "FORWARDAIR"
    assert res.bill_to == "CALIX"
    assert res.status == "IN_TRANSIT"
    assert res.raw_status == "On-time"
    assert res.delivered is False
    assert res.origin == "ORD"
    assert res.destination == "ONT"
    assert res.pieces == 9
    assert res.weight == 1120.0
    assert res.shipper == "ORD"
    assert res.consignee == "ONT"


# 19. Forward Air not found error
def test_forwardair_not_found(forwardair_extractor):
    nf_html = "<html><body><div class='fwrd-listing-result'><div class='no-results'>No shipments found for 99999999</div></div></body></html>"
    with pytest.raises(NotFoundError) as exc_info:
        forwardair_extractor.extract(nf_html, "99999999")
    assert "99999999" in str(exc_info.value)

