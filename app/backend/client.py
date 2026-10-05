"""Backend API client (mockable). FileBackend stubs both APIs from local JSON using the SAME parsing
as the HTTP client, so the stub exercises the real code path."""
from __future__ import annotations

import importlib
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from app.core.postprocess import get_path
from app.models import CarrierAccount, PendingBatch, PendingRef
from app.settings import BackendSettings, CarrierSpec


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
        self.http = httpx.AsyncClient(base_url=s.base_url, timeout=30,
                                      headers={"Authorization": f"Bearer {token}"} if token else {})

    async def get_carrier_accounts(self):
        r = await self.http.get(self.s.carriers_path)
        r.raise_for_status()
        return _accounts(r.json())

    async def get_pending(self, account, spec):
        cfg = spec.pending
        if cfg.get("plugin"):  # fully custom per-carrier call: "pkg.mod:async_fn(client, account, spec)"
            mod, fn = cfg["plugin"].split(":")
            return await getattr(importlib.import_module(mod), fn)(self.http, account, spec)
        url = cfg["endpoint"].format(bill_to=account.bill_to, carrier_code=spec.code)
        r = await self.http.request(cfg.get("method", "GET"), url, params=cfg.get("params"))
        r.raise_for_status()
        return parse_pending(r.json(), account, spec)

    async def post_tracking(self, account, spec, run_id, items):
        r = await self.http.post(self.s.post_path, json={"runId": run_id, "billTo": account.bill_to,
                                                         "carrierCode": spec.code, "items": items})
        r.raise_for_status()


def make_backend(s: BackendSettings) -> BackendClient:
    return FileBackend(s) if s.mode == "file" else HttpBackend(s)
