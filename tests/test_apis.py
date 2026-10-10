import pytest

from app.backend.apis import parse_configurations, parse_shipments, CarrierConfigurationBackend, ApisBackend
from app.settings import load_resolver, CarrierConfigurationSettings


def _config(code="SAI001", active=True):
    return {
        "id": 1,
        "carrierName": "SAIA MOTOR FREIGHT LINE",
        "carrierCode": code,
        "trackingUrl": "https://www.saia.com/track",
        "billTo": 134034,
        "customerName": "CALIX NETWORKS",
        "processingFrequency": 0,
        "processingDelay": 0,
        "userName": "user",
        "password": "secret",
        "isActive": active,
    }


def test_multiple_configs_and_inactive_filter():
    accounts = parse_configurations([_config(), _config("SAI002", False)])
    assert [a.carrier_code for a in accounts if a.is_active] == ["SAI001"]
    assert accounts[0].password.get_secret_value() == "secret"


def test_empty_configs_and_malformed_rows():
    assert parse_configurations([]) == []
    assert parse_configurations([{}, _config()])[0].carrier_code == "SAI001"


def test_shipments_keep_references_as_strings_and_deduplicate():
    account = parse_configurations([_config()])[0]
    spec = load_resolver("app/clients").resolve(account.bill_to, account.carrier_code)
    batch = parse_shipments(
        [
            {"housebill": 23460580, "proNumber": 771043317403, "vendorId": "SAI001"},
            {"housebill": "23460580", "proNumber": "771043317403", "vendorId": "SAI001"},
        ],
        account,
        spec,
    )
    assert len(batch.refs) == 1
    ref = batch.refs[0]
    assert ref.reference == "771043317403" and ref.ref_type == "PRO"
    assert ref.meta["housebill"] == "23460580" and ref.meta["vendorId"] == "SAI001"
    assert parse_shipments([], account, spec).refs == []


@pytest.mark.asyncio
async def test_shipment_request_uses_bill_to_and_api_carrier_code():
    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return []

    class FakeHttp:
        def build_request(self, method, path, **kwargs):
            import httpx

            return httpx.Request(
                method,
                "https://plgtst.pegasuslogistics.com/api/orderentry" + path,
                json=kwargs.get("json"),
                params=kwargs.get("params"),
            )

        async def send(self, request):
            assert request.method == "GET"
            assert request.url.path.endswith("GetCarrierShipmentDetails")
            assert request.content == b'{"billTo":"134034","carrierCode":"SAI001"}'
            return Response()

        async def aclose(self):
            pass

    class Output:
        async def post_tracking(self, *args):
            pass

    client = ApisBackend(CarrierConfigurationSettings(), Output())
    await client.http.aclose()
    client.http = FakeHttp()
    account = parse_configurations([_config()])[0]
    spec = load_resolver("app/clients").resolve(account.bill_to, account.carrier_code)
    assert (await client.get_pending(account, spec)).refs == []
