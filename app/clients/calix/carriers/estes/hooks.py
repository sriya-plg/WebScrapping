"""Calix x Estes payload tweaks. Auto-used if this file defines post(payload, result, account)."""


def post(payload: dict, result, account) -> dict:
    payload["housebill"] = payload.get("housebill") or result.reference
    for k in ("deliveryDate", "eta"):
        if payload.get(k):
            payload[k] = str(payload[k])[:10]
    return payload
