"""Config-driven normalization + payload mapping (+ optional per-carrier hook)."""
from __future__ import annotations

import importlib
from typing import Any

from app.models import CarrierAccount, TrackingEvent, TrackingResult
from app.settings import CarrierSpec


def get_path(obj: Any, path: str, default: Any = None) -> Any:
    """Dotted path with list indexes; 'a.b|c.d' = first non-empty alternative."""
    for alt in path.split("|"):
        cur, ok = obj, True
        for part in alt.strip().split("."):
            if isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                except (ValueError, IndexError):
                    ok = False
                    break
            elif isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, "", []):
            return cur
    return default


def normalize_with_config(spec: CarrierSpec, raw: dict, ref: str, ref_type: str,
                          account: CarrierAccount) -> TrackingResult:
    n = spec.normalize
    vals = {f: get_path(raw, p) for f, p in n.fields.items()}
    raw_status = vals.pop("status", None)
    status = n.status_map.get(str(raw_status).strip().lower(), "UNKNOWN") if raw_status else "UNKNOWN"
    events: list[TrackingEvent] = []
    if n.events:
        for item in get_path(raw, n.events["path"], []) or []:
            events.append(TrackingEvent(**{f: get_path(item, p) for f, p in n.events["fields"].items()}))
    allowed = set(TrackingResult.model_fields) - {"reference", "ref_type", "carrier_code", "bill_to", "status",
                                                  "raw_status", "delivered", "events", "raw", "scraped_at"}
    return TrackingResult(
        reference=ref, ref_type=ref_type, carrier_code=spec.code, bill_to=account.bill_to,
        status=status, raw_status=str(raw_status) if raw_status else None, delivered=status == "DELIVERED",
        events=events, raw=raw, **{k: v for k, v in vals.items() if k in allowed})


def _load_hook(path: str):
    mod, fn = path.split(":")
    return getattr(importlib.import_module(mod), fn)


def build_payload(spec: CarrierSpec, result: TrackingResult, account: CarrierAccount, run_id: str,
                  meta: dict | None = None) -> dict:
    data = result.model_dump(mode="json", exclude={"raw"})
    data["ctx"] = {"bill_to": account.bill_to, "carrier_code": spec.code, "customer_name": account.customer_name,
                   "run_id": run_id, **(meta or {})}
    payload: dict[str, Any] = {}
    for key, path in spec.payload_map.items():
        payload[key] = path[1:] if path.startswith("=") else get_path(data, path)
    if spec.post_hook:
        payload = _load_hook(spec.post_hook)(payload, result, account) or payload
    return payload
