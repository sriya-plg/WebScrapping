"""External APIs adapter (OrderEntry carrier configurations, pending shipments, and tracking endpoints).

Acts as a BackendClient decorator: fetches carrier accounts and shipment inputs via HTTP APIs,
and delegates tracking output to the configured backend.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.backend.client import BackendClient, parse_pending
from app.core.resilience import retry
from app.logging_setup import log
from app.models import CarrierAccount, PendingBatch
from app.settings import CarrierConfigurationSettings, CarrierSpec

logger = logging.getLogger(__name__)


def _rows(data: Any, label: str) -> list[dict]:
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("data", data.get("items"))
    else:
        rows = None
    if not isinstance(rows, list):
        raise ValueError(f"carrier configuration API {label} response must contain an array")
    malformed = sum(not isinstance(row, dict) for row in rows)
    if malformed:
        logger.warning(
            "ignoring malformed carrier configuration API records",
            extra={"fields": {"kind": label, "count": malformed}},
        )
    return [row for row in rows if isinstance(row, dict)]


def parse_configurations(data: Any) -> list[CarrierAccount]:
    """Validate API records independently; malformed records don't discard valid ones."""
    result = []
    for row in _rows(data, "configuration"):
        try:
            result.append(CarrierAccount.model_validate(row))
        except Exception:
            logger.warning("ignoring malformed carrier configuration API record")
    return [account.model_copy(update={"from_carrier_configuration": True}) for account in result]


def parse_shipments(data: Any, account: CarrierAccount, spec: CarrierSpec) -> PendingBatch:
    rows = _rows(data, "shipment")
    # API's PRO is the specified search reference. Keep housebill available as metadata.
    records = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        pro, bill = row.get("proNumber"), row.get("housebill")
        if pro in (None, ""):
            continue
        pro = str(pro)
        key = (account.bill_to, pro)
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "proNumber": pro,
            "housebill": str(bill) if bill is not None else "",
            "vendorId": str(row.get("vendorId", "")),
        })
    return parse_pending({"data": records}, account, spec)


class CarrierConfigurationBackend(BackendClient):
    """Fetches carrier configuration and shipment inputs, delegating output to the existing backend."""

    def __init__(self, cfg: CarrierConfigurationSettings, output: BackendClient):
        self.cfg, self.output = cfg, output
        self.http = httpx.AsyncClient(base_url=cfg.base_url.rstrip("/"), timeout=cfg.timeout_s)

    async def _get_json(self, path: str, *, body: dict | None = None):
        path = path.lstrip("/")  # keep configured API path beneath base_url's /api/orderentry prefix
        attempts = 0

        async def request():
            nonlocal attempts
            attempts += 1
            t0 = time.monotonic()
            try:
                if body is None:
                    response = await self.http.get(path)
                elif self.cfg.shipments_method == "POST":
                    response = await self.http.post(path, json=body)
                else:
                    # GET with JSON body follows the supplied contract; configurable because support is unverified.
                    request = (
                        self.http.build_request("GET", path, json=body)
                        if self.cfg.shipments_get_json_body
                        else self.http.build_request("GET", path, params=body)
                    )
                    response = await self.http.send(request)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.NetworkError(f"transient carrier configuration API status {response.status_code}")
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                raise httpx.NetworkError("transient carrier configuration API error") from exc
            finally:
                log(
                    logger,
                    logging.INFO,
                    "carrier configuration API request completed",
                    path=path,
                    duration_s=round(time.monotonic() - t0, 2),
                    retry_count=max(0, attempts - 1),
                )

        return await retry(
            request,
            attempts=self.cfg.retries,
            base=1,
            cap=8,
            retry_on=(httpx.NetworkError, httpx.TimeoutException),
        )

    async def get_carrier_accounts(self) -> list[CarrierAccount]:
        data = await self._get_json(self.cfg.configurations_path)
        accounts = parse_configurations(data)
        active = [account for account in accounts if account.is_active]
        log(
            logger,
            logging.INFO,
            "carrier configurations fetched",
            received=len(accounts),
            active=len(active),
            inactive=len(accounts) - len(active),
        )
        return active

    async def get_pending(self, account: CarrierAccount, spec: CarrierSpec) -> PendingBatch:
        body = {"billTo": account.bill_to, "carrierCode": account.carrier_code}
        data = await self._get_json(self.cfg.shipments_path, body=body)
        batch = parse_shipments(data, account, spec)
        log(
            logger,
            logging.INFO,
            "carrier shipments fetched",
            carrier_code=account.carrier_code,
            bill_to=account.bill_to,
            count=len(batch.refs),
        )
        return batch

    async def post_tracking(self, account, spec, run_id, items):
        await self.output.post_tracking(account, spec, run_id, items)

    async def close(self):
        await self.http.aclose()


# Alias for cleaner naming
ApisBackend = CarrierConfigurationBackend
