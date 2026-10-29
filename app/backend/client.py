"""HTTP-only backend integration. The backend owns carrier settings, shipment intake and deduplication."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from app.backend.credentials import make_credential_resolver
from app.logging_setup import register_secret
from app.models import CarrierConfig, ShipmentRef
from app.settings import BackendSettings

logger = logging.getLogger(__name__)


class BackendError(RuntimeError):
    pass


class BackendAuthError(BackendError):
    pass


class BackendUnavailable(BackendError):
    pass


class BackendSchemaError(BackendError):
    pass


class HttpBackend:
    def __init__(self, settings: BackendSettings):
        self.settings = settings
        token = os.environ.get(settings.token_env, "")
        base_url = os.environ.get("TRACKER_BASE_URL", settings.base_url).rstrip("/")
        headers = ({settings.auth_header: f"{settings.auth_scheme} {token}" if settings.auth_scheme else token}
                   if token else {})
        self.http = httpx.AsyncClient(base_url=base_url, timeout=settings.timeout_seconds, headers=headers)
        activity_headers = headers if settings.carrier_activity_auth else {}
        self.http_activity = httpx.AsyncClient(base_url=base_url, timeout=settings.timeout_seconds,
                                               headers=activity_headers)
        resolver_override = os.environ.get("CREDENTIAL_RESOLVER")
        resolver_name = resolver_override or settings.credential_resolver
        if not resolver_override and resolver_name == "identity" and os.environ.get("CARRIER_CREDENTIALS_KEY"):
            resolver_name = "fernet"
        self.credentials = make_credential_resolver(resolver_name)

    async def _request_json(self, method: str, path: str, **kwargs) -> Any:
        for attempt in range(max(1, self.settings.retry_attempts)):
            try:
                response = await self.http.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 >= max(1, self.settings.retry_attempts):
                    raise BackendUnavailable("backend request failed or timed out") from exc
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            if response.status_code in (401, 403):
                raise BackendAuthError(f"backend authentication failed ({response.status_code})")
            if response.status_code >= 500:
                if attempt + 1 < max(1, self.settings.retry_attempts):
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                raise BackendUnavailable(f"backend unavailable ({response.status_code})")
            if response.is_error:
                raise BackendError(f"backend rejected request ({response.status_code})")
            try:
                return response.json()
            except ValueError as exc:
                raise BackendSchemaError("backend response was not valid JSON") from exc
        raise BackendUnavailable("backend unavailable")

    @staticmethod
    def _rows(data: Any, label: str) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "items", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        raise BackendSchemaError(f"{label} response must contain an array")

    async def get_carrier_configs(self) -> list[CarrierConfig]:
        data = await self._request_json(self.settings.carrier_config_method,
                                        self.settings.carrier_config_path)
        configs: list[CarrierConfig] = []
        for row in self._rows(data, "carrier configuration"):
            try:
                config = CarrierConfig.model_validate(row)
                if config.password:
                    encrypted = config.password.get_secret_value()
                    register_secret(encrypted)
                    plaintext = self.credentials.decrypt(encrypted)
                    config.password = type(config.password)(plaintext)
                    register_secret(plaintext)
                configs.append(config)
            except Exception as exc:
                # Do not include the original row or validation exception: it may contain credentials.
                raise BackendSchemaError("invalid carrier configuration item or credential decryption failed") from None
        return configs

    async def get_shipments(self, bill_to: int, carrier_code: str) -> list[ShipmentRef]:
        data = await self._request_json("POST", self.settings.shipments_path,
                                        json={"billTo": bill_to, "carrierCode": carrier_code})
        rows = self._rows(data, "shipment")
        shipments: list[ShipmentRef] = []
        for row in rows:
            try:
                shipments.append(ShipmentRef.model_validate(row))
            except Exception:
                logger.warning("skipping invalid shipment row | bill_to=%s carrier_code=%s", bill_to, carrier_code)
        return shipments

    async def add_carrier_activity(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.http_activity.post(self.settings.carrier_activity_path, json=payload)
        try:
            body: Any = response.json()
        except ValueError:
            body = (response.text or "")[:2000]
        result = {"ok": not response.is_error, "status_code": response.status_code, "body": body}
        if response.is_error:
            logger.warning("backend activity update rejected | status=%s housebill=%s bill_to=%s",
                           response.status_code, payload.get("housebill"), payload.get("billTo"))
            response.raise_for_status()
        return result

    async def close(self) -> None:
        await self.http.aclose()
        await self.http_activity.aclose()


def make_backend(settings: BackendSettings) -> HttpBackend:
    return HttpBackend(settings)
