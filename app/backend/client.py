"""Backend API client (mockable). FileBackend stubs both APIs from local JSON using the SAME parsing
as the HTTP client, so the stub exercises the real code path."""
from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from app.backend.credentials import IdentityCredentialResolver, make_credential_resolver
from app.core.mapper import get_path
from app.logging_setup import register_secret
from app.models import (
    CarrierAccount,
    CarrierConfig,
    PendingBatch,
    PendingRef,
    ShipmentRef,
)
from app.settings import BackendSettings, CarrierSpec


class BackendError(Exception): pass
class BackendAuthError(BackendError): pass
class BackendUnavailable(BackendError): pass
class BackendSchemaError(BackendError): pass


def parse_pending(data: Any, account: CarrierAccount, spec: CarrierSpec) -> PendingBatch:
    """Config-driven parse of the per-carrier 'pending refs' response.
    spec.pending: items_path, ref_field, ref_type_field | search_by_path | search_by_default."""
    cfg = spec.pending
    items = get_path(data, cfg["items_path"], []) if cfg.get("items_path") else data
    search_by = (get_path(data, cfg["search_by_path"]) if cfg.get("search_by_path") else None) \
        or cfg.get("search_by_default", "PRO")
    refs = []
    for it in items or []:
        ref = get_path(it, cfg.get("ref_field", "reference"))
        if ref:
            rt = get_path(it, cfg["ref_type_field"]) if cfg.get("ref_type_field") else None
            refs.append(PendingRef(reference=str(ref), ref_type=str(rt or search_by).upper(),
                                   meta={k: v for k, v in it.items() if isinstance(v, (str, int, float))}
                                   if isinstance(it, dict) else {}))
    return PendingBatch(bill_to=account.bill_to, carrier_code=spec.code, search_by=str(search_by).upper(), refs=refs)


class BackendClient(ABC):
    @abstractmethod
    async def get_carrier_accounts(self) -> list[CarrierAccount]: ...
    @abstractmethod
    async def get_pending(self, account: CarrierAccount, spec: CarrierSpec) -> PendingBatch: ...
    @abstractmethod
    async def post_tracking(self, account: CarrierAccount, spec: CarrierSpec, run_id: str,
                            items: list[dict]) -> None: ...


def _accounts(data: Any) -> list[CarrierAccount]:
    rows = data.get("data", data.get("items", [])) if isinstance(data, dict) else data
    return [CarrierAccount(**r) for r in rows]


class FileBackend(BackendClient):
    """stub/carriers.json, stub/pending/<BILLTO>_<CODE>.json; posts land in out/<run>/..."""

    def __init__(self, s: BackendSettings):
        self.stub, self.out = Path(s.stub_dir), Path(s.outbox_dir)

    async def get_carrier_accounts(self):
        return _accounts(json.loads((self.stub / "carriers.json").read_text()))

    async def get_pending(self, account, spec):
        f = self.stub / "pending" / f"{account.bill_to}_{spec.code}.json"
        data = json.loads(f.read_text()) if f.exists() else []
        return parse_pending(data, account, spec)

    async def post_tracking(self, account, spec, run_id, items):
        d = self.out / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{account.bill_to}_{spec.code}.json").write_text(json.dumps(items, indent=2))


class HttpBackend(BackendClient):
    def __init__(self, s: BackendSettings):
        self.s = s
        token = os.environ.get(s.token_env, "")
        base_url = os.environ.get("TRACKER_BASE_URL", s.base_url)
        self.http = httpx.AsyncClient(base_url=base_url, timeout=s.timeout_seconds,
                                      headers={s.auth_header: f"{s.auth_scheme} {token}" if s.auth_scheme else token}
                                      if token else {})
        resolver_name = os.environ.get("CREDENTIAL_RESOLVER")
        if resolver_name:
            self.credentials = make_credential_resolver(resolver_name)
        elif s.credential_resolver and s.credential_resolver != "identity":
            self.credentials = make_credential_resolver(s.credential_resolver)
        elif os.environ.get("CARRIER_CREDENTIALS_KEY"):
            self.credentials = make_credential_resolver("fernet")
        else:
            self.credentials = make_credential_resolver("identity")

    async def _request_json(self, method: str, path: str, **kwargs):
        for attempt in range(self.s.retry_attempts):
            try:
                response = await self.http.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                if attempt + 1 == self.s.retry_attempts:
                    raise BackendUnavailable("backend request timed out") from exc
                await asyncio.sleep(2 ** attempt)
                continue
            except httpx.HTTPError as exc:
                raise BackendUnavailable("backend connection failed") from exc
            if response.status_code in (401, 403):
                raise BackendAuthError(f"backend authentication failed ({response.status_code})")
            if response.status_code >= 500:
                if attempt + 1 < self.s.retry_attempts:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise BackendUnavailable(f"backend unavailable ({response.status_code})")
            if response.is_error:
                raise BackendError(f"backend rejected request ({response.status_code})")
            try:
                return response.json()
            except ValueError as exc:
                raise BackendSchemaError("backend response was not valid JSON") from exc
        raise BackendUnavailable("backend unavailable")

    async def get_carrier_configs(self) -> list[CarrierConfig]:
        data = await self._request_json(self.s.carrier_config_method, self.s.carrier_config_path)
        if not isinstance(data, list):
            raise BackendSchemaError("carrier configuration response must be an array")
        configs = []
        for item in data:
            try:
                config = CarrierConfig.model_validate(item)
                if config.password:
                    password = config.password.get_secret_value()
                    register_secret(password)
                    config.password = type(config.password)(self.credentials.decrypt(password))
                    register_secret(config.password.get_secret_value())
                configs.append(config)
            except Exception as exc:
                raise BackendSchemaError("invalid carrier configuration item") from exc
        return configs

    async def get_carrier_accounts(self):
        configs = await self.get_carrier_configs()
        accounts = []
        for c in configs:
            try:
                password = c.password.get_secret_value() if c.password else None
                accounts.append(CarrierAccount(id=c.id, carrierName=c.carrier_name, carrierCode=c.carrier_code,
                    trackingUrl=c.tracking_url, billTo=str(c.bill_to), customerName=c.customer_name,
                    processingFrequency=c.processing_frequency, processingDelay=c.processing_delay,
                    userName=c.user_name, password=password, isActive=c.is_active, searchBy=c.search_by))
            except Exception as exc:
                raise BackendSchemaError("invalid carrier configuration item") from exc
        return accounts

    async def get_shipments(self, bill_to: int, carrier_code: str) -> list[ShipmentRef]:
        data = await self._request_json("POST", self.s.shipments_path,
                                        json={"billTo": bill_to, "carrierCode": carrier_code})
        if not isinstance(data, list):
            raise BackendSchemaError("shipment response must be an array")
        try:
            return [ShipmentRef.model_validate(row) for row in data]
        except Exception as exc:
            raise BackendSchemaError("invalid shipment item") from exc

    async def get_pending(self, account, spec):
        rows = await self.get_shipments(int(account.bill_to), spec.code)
        seen: set[str] = set()
        refs = []
        skipped = 0
        for row in rows:
            search_by = account.search_by or "proNumber"
            value = str(getattr(row, search_by, None) or getattr(row, {
                "proNumber": "pro_number", "housebill": "housebill", "vendorId": "vendor_id"
            }.get(search_by, "pro_number"), "") or "").strip()
            key = value.casefold()
            if not value or key in seen:
                skipped += 1
                continue
            seen.add(key)
            refs.append(PendingRef(reference=value, ref_type=search_by.upper(),
                                   meta={"housebill": row.housebill, "vendorId": row.vendor_id}))
        if skipped:
            import logging
            logging.getLogger(__name__).info("shipment rows skipped", extra={"fields": {
                "bill_to": account.bill_to, "carrier_code": spec.code, "skipped": skipped}})
        ref_type = {"pronumber": "PRO", "housebill": "BOL"}.get(account.search_by.casefold(),
                                                                         account.search_by.upper())
        refs = [r.model_copy(update={"ref_type": ref_type}) for r in refs]
        return PendingBatch(bill_to=account.bill_to, carrier_code=spec.code, search_by=ref_type, refs=refs)

    async def post_tracking(self, account, spec, run_id, items):
        r = await self.http.post(self.s.post_path, json={"runId": run_id, "billTo": account.bill_to,
                                                         "carrierCode": spec.code, "items": items})
        r.raise_for_status()


def make_backend(s: BackendSettings) -> BackendClient:
    return FileBackend(s) if s.mode == "file" else HttpBackend(s)
