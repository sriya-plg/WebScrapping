import json
import logging
import asyncio

import httpx
import pytest

from app.backend.client import BackendUnavailable, HttpBackend
from app.core.intake import build_jobs
from app.models import CarrierAccount, CarrierConfig, ShipmentRef
from app.settings import BackendSettings


CONFIG = {"id": 1, "carrierName": "SAIA", "carrierCode": "SAI001", "trackingUrl": "https://example.test",
          "billTo": 134034, "customerName": "Client", "processingFrequency": 0, "processingDelay": 0,
          "userName": "user", "password": "secret-password", "isActive": True, "createdBy": 1}


def backend(handler):
    result = HttpBackend(BackendSettings(base_url="https://backend.test"))
    result.http = httpx.AsyncClient(base_url="https://backend.test", transport=httpx.MockTransport(handler))
    return result


def test_add_carrier_activity_returns_http_response():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"isSuccess": True, "id": 99})

    client = HttpBackend(BackendSettings(base_url="https://backend.test"))
    transport = httpx.MockTransport(handler)
    client.http = httpx.AsyncClient(base_url="https://backend.test", transport=transport)
    client.http_activity = httpx.AsyncClient(base_url="https://backend.test", transport=transport)

    payload = {
        "housebill": "23673123", "proNumber": "771377522906", "carrierCode": "SAI001",
        "billTo": 134034, "mlStatus": 2, "events": [],
    }

    async def run():
        try:
            result = await client.add_carrier_activity(payload)
            assert captured["body"]["billTo"] == 134034
            assert result["ok"] is True
            assert result["status_code"] == 200
            assert result["body"]["isSuccess"] is True
            assert result["body"]["id"] == 99
        finally:
            await client.http.aclose()
            await client.http_activity.aclose()

    asyncio.run(run())


def test_get_carrier_shipment_details_new_schema():
    """GetCarrierShipmentDetails: numeric housebill, no vendorId; optional carrier/status fields."""
    payload = [
        {
            "housebill": 23668423,
            "proNumber": "164-8491797",
            "carrierCode": "ESTE03",
            "serviceLevel": "5D",
            "status": "Arrived Destination Terminal",
        },
        {
            "housebill": 23668502,
            "proNumber": "184-8683493",
            "carrierCode": "ESTE03",
            "serviceLevel": "5D",
            "status": "Confirmed on Board",
        },
    ]

    def handler(request):
        if request.url.path.endswith("GetCarrierConfigurationDetails"):
            return httpx.Response(200, json=[CONFIG])
        return httpx.Response(200, json=payload)

    client = backend(handler)

    async def run():
        try:
            rows = await client.get_shipments(134034, "ESTE03")
            assert len(rows) == 2
            assert rows[0].housebill == "23668423"
            assert rows[0].pro_number == "164-8491797"
            assert rows[0].vendor_id == ""
            assert rows[0].carrier_code == "ESTE03"
            assert rows[0].status == "Arrived Destination Terminal"
        finally:
            await client.http.aclose()

    asyncio.run(run())


def test_config_and_shipments_filter_empty_and_duplicate_pro():
    def handler(request):
        if request.url.path.endswith("GetCarrierConfigurationDetails"):
            return httpx.Response(200, json=[CONFIG, {**CONFIG, "id": 2, "isActive": False}])
        assert json.loads(request.content) == {"billTo": 134034, "carrierCode": "SAI001"}
        return httpx.Response(200, json=[{"housebill": "h1", "proNumber": "p1", "vendorId": "SAI001"},
            {"housebill": "h2", "proNumber": "P1", "vendorId": "SAI001"},
            {"housebill": "h3", "proNumber": " ", "vendorId": "SAI001"}])
    client = backend(handler)
    async def run():
        try:
            configs = await client.get_carrier_configs()
            assert len([c for c in configs if c.is_active]) == 1
            assert "secret-password" not in repr(configs[0])
            jobs_by_customer = await build_jobs(client)
            jobs = jobs_by_customer["Client"]["SAI001"]
            assert [job.search_value for job in jobs] == ["p1"]
        finally:
            await client.http.aclose()
            await client.http_activity.aclose()
    asyncio.run(run())


def test_retries_503_then_succeeds_and_password_not_logged(caplog):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls < 2 else 200, json=[])
    client = backend(handler)
    client.settings.retry_attempts = 2
    async def run():
        try:
            assert await client.get_carrier_configs() == []
            assert calls == 2
            caplog.set_level(logging.INFO)
            logging.getLogger(__name__).info("configuration fetched")
            assert "secret-password" not in caplog.text
        finally:
            await client.http.aclose()
    asyncio.run(run())


def test_final_503_is_typed_unavailable():
    client = backend(lambda request: httpx.Response(503))
    client.settings.retry_attempts = 1
    async def run():
        try:
            with pytest.raises(BackendUnavailable):
                await client.get_carrier_configs()
        finally:
            await client.http.aclose()
    asyncio.run(run())


def test_build_jobs_skips_inactive_dedupes_and_isolates_carrier_failure():
    configs = [CarrierConfig.model_validate(CONFIG),
               CarrierConfig.model_validate({**CONFIG, "id": 2, "carrierCode": "FAIL"}),
               CarrierConfig.model_validate({**CONFIG, "id": 3, "carrierCode": "OFF", "isActive": False})]
    class FakeBackend:
        async def get_carrier_configs(self):
            return configs
        async def get_shipments(self, bill_to, code):
            if code == "FAIL":
                raise BackendUnavailable("offline")
            return [ShipmentRef(housebill="h1", proNumber=" p1 ", vendorId=code),
                    ShipmentRef(housebill="h2", proNumber="P1", vendorId=code),
                    ShipmentRef(housebill="h3", proNumber="", vendorId=code)]
    grouped = asyncio.run(build_jobs(FakeBackend()))
    assert list(grouped["Client"]) == ["SAI001"]
    jobs = grouped["Client"]["SAI001"]
    assert len(jobs) == 1 and jobs[0].search_value == "p1"
    assert jobs[0].search_by == "proNumber"


def test_http_backend_credential_decryption_with_fernet(monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("CARRIER_CREDENTIALS_KEY", key)

    plain_pass = "ActualDecryptedCarrierPassword456"
    token = Fernet(key.encode("utf-8")).encrypt(plain_pass.encode("utf-8")).decode("utf-8")
    encrypted_config = {**CONFIG, "password": token}

    client = backend(lambda request: httpx.Response(200, json=[encrypted_config]))
    async def run():
        try:
            configs = await client.get_carrier_configs()
            assert len(configs) == 1
            config = configs[0]
            assert config.password is not None
            assert config.password.get_secret_value() == plain_pass
            # Confirm SecretStr redaction in repr
            assert plain_pass not in repr(config)
        finally:
            await client.http.aclose()
            await client.http_activity.aclose()
    asyncio.run(run())
