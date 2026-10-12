"""Pure functions (no I/O): raw carrier JSON -> common schema -> backend payload.
Stage 1 (carrier-specific):  normalize()      mapping.yaml fields/events (auto-detection fills gaps)
Stage 2 (client-specific):   apply_map()      client.yaml payload_map / activity.payload_map / events_map
                                             (+ optional hooks.py)"""
from __future__ import annotations

import importlib
import re
from collections import deque
from datetime import datetime
from typing import Any, Iterator

from app.models import CarrierAccount, TrackingEvent, TrackingResult
from app.settings import CarrierSpec


def get_path(obj: Any, path: str, default: Any = None) -> Any:
    """Dotted path with list indexes; 'a.b|c.d' = first non-empty alternative."""
    for alt in path.split("|"):
        cur, ok = obj, True
        for part in alt.strip().split("."):
            if isinstance(cur, dict) and "data" in cur and len(cur) == 1 and part.strip("[]").isdigit():
                cur = cur["data"]
            if isinstance(cur, list):
                try:
                    cur = cur[int(part.strip("[]"))]
                except (ValueError, IndexError):
                    ok = False
                    break
            elif isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, dict) and part.strip("[]").isdigit() and int(part.strip("[]")) == 0:
                continue
            else:
                ok = False
                break
        if ok and cur not in (None, "", []):
            return cur
    return default


_COMPOSE_RX = re.compile(r"\{([^}]+)\}")


def compose_value(obj: Any, template: str) -> Any:
    """Fill `{dotted.path}` placeholders from obj. Used by mapping.yaml `compose:`."""
    if not template:
        return None
    if "{" not in template:
        return get_path(obj, template)
    def repl(match: re.Match) -> str:
        value = get_path(obj, match.group(1).strip())
        return "" if value in (None, "") else str(value)
    text = _COMPOSE_RX.sub(repl, template)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    return text or None


def parse_loose_datetime(value: Any) -> str | None:
    """Turn Saia-style `2026-09-15` + `12:13 pm` (or already-ISO) into ISO-8601."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    text = re.sub(r"\s+", " ", str(value).strip())
    if not text:
        return None
    text = text.replace("Z", "")
    text = re.sub(r"\b([ap])\.?m\.?\b", lambda m: m.group(1).upper() + "M", text, flags=re.I)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %I:%M%p",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(microsecond=0).isoformat()
    except ValueError:
        return text


def _nk(k: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


# ---------------------------------------------------------------------------------- auto-detection
_FIELD_RX = {k: re.compile(v) for k, v in {
    "status": r"^(shipment|current|tracking|freight|load|pro|delivery)?status(desc|description|text|name|message)?$",
    "pickup_date": r"^(pickup|pickedup|pu)(date|dt|datetime|time)?$",
    "delivery_date": r"^(delivery|delivered|deliv|dlv|actualdelivery)(date|dt|datetime)$",
    "eta": r"^(eta|estimateddelivery(date)?|appointment(date|time)?|scheduleddelivery(date)?|"
           r"expecteddelivery(date)?|promiseddelivery(date)?)$",
    "origin": r"^(origin|originlocation|origincity|originterminal|shipperlocation|shippercity)$",
    "destination": r"^(destination|destinationlocation|destinationcity|destinationterminal|"
                   r"consigneelocation|consigneecity|dest)$",
    "pieces": r"^(pieces|piececount|pcs|totalpieces|handlingunits|handlingunitcount)$",
    "weight": r"^(weight|totalweight|grossweight|shipmentweight)$",
    "service_level": r"^(servicelevel|servicetype|service)$",
    "pod_name": r"^(podname|signedby|signature|receivedby|signee|podsignature)$",
    "pod_datetime": r"^(poddatetime|poddate|signeddate|deliverysignaturedate)$",
}.items()}
_EV_LIST_RX = re.compile(r"(history|events?|activity|activities|trace|tracking|milestones?|scans?|statuses|"
                         r"details|checkpoints?|progress)")
_EV_CANDIDATES = {
    "timestamp": ["timestamp", "datetime", "eventdate", "eventtime", "scandate", "statusdate", "date", "time", "dt"],
    "description": ["description", "desc", "eventdescription", "event", "activity", "message",
                    "statusdescription", "status", "text"],
    "location": ["location", "eventlocation", "city", "terminal", "facility", "site", "place"],
    "status": ["status", "statuscode", "eventtype", "type"],
    "code": ["code", "statuscode", "eventcode", "eventtype", "type"],
    "timezone": ["timezone", "tz", "timezonename"],
    "lat": ["lat", "latitude"],
    "long": ["long", "lng", "longitude"],
}


def _flatten(raw: Any, max_nodes: int = 3000) -> list[tuple[str, str, Any]]:
    """Scalar leaves as (path, key, value), shallowest first. Only descends into the first item of lists."""
    out, q, n = [], deque([("", raw)]), 0
    while q and n < max_nodes:
        path, obj = q.popleft()
        n += 1
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}.{k}" if path else str(k)
                if isinstance(v, dict):
                    q.append((p, v))
                elif isinstance(v, list):
                    if v and isinstance(v[0], dict):
                        q.append((f"{p}.0", v[0]))
                elif v not in (None, "") and not isinstance(v, bool):
                    out.append((p, str(k), v))
        elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
            q.append((f"{path}.0" if path else "0", obj[0]))
    return out


def auto_fields(raw: Any) -> dict[str, tuple[str, Any]]:
    flat, found = _flatten(raw), {}
    for f, rx in _FIELD_RX.items():
        for p, k, v in flat:
            if rx.match(_nk(k)) and isinstance(v, (str, int, float)):
                found[f] = (p, v)
                break
    return found


def _lists_of_dicts(obj: Any, path: str = "", depth: int = 0) -> Iterator[tuple[str, list]]:
    if depth > 6:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _lists_of_dicts(v, f"{path}.{k}" if path else str(k), depth + 1)
    elif isinstance(obj, list):
        if obj and all(isinstance(i, dict) for i in obj[:3]):
            yield path, obj
        for i, v in enumerate(obj[:1]):
            yield from _lists_of_dicts(v, f"{path}.{i}", depth + 1)


def _pick_key(item: dict, candidates: list[str]) -> Any:
    by_nk = {_nk(k): v for k, v in item.items() if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
    for c in candidates:
        if by_nk.get(c) not in (None, ""):
            return by_nk[c]
    return None


def auto_events(raw: Any) -> tuple[str, list[TrackingEvent]] | None:
    best, best_score = None, 0
    for path, lst in _lists_of_dicts(raw):
        keys = {_nk(k) for k in lst[0]}
        score = (3 if _EV_LIST_RX.search(_nk(path.split(".")[-1])) else 0) \
            + (2 if keys & set(_EV_CANDIDATES["timestamp"]) else 0) \
            + (2 if keys & set(_EV_CANDIDATES["description"]) else 0)
        if score > best_score:
            best, best_score = (path, lst), score
    if not best or best_score < 4:
        return None
    return best[0], [TrackingEvent(**{f: _pick_key(i, c) for f, c in _EV_CANDIDATES.items()}) for i in best[1]]


# ------------------------------------------------------------------------------------------ status
def status_from_text(text: str, status_map: dict[str, str]) -> str:
    t = text.strip().lower()
    lowmap = {k.strip().lower(): v for k, v in status_map.items()}   # YAML keys may be any case
    if t in lowmap:
        return lowmap[t]
    for k, v in lowmap.items():
        if k and k in t:
            return v
    if re.search(r"out for deliver|on vehicle for deliver|with driver", t):
        return "OUT_FOR_DELIVERY"
    if re.search(r"\bdeliver(ed|y complete)", t) and not re.search(r"attempt|schedul|appointment|not deliver", t):
        return "DELIVERED"
    if re.search(r"exception|delay|attempt|refus|damag|missed|on hold|claim", t):
        return "EXCEPTION"
    if re.search(r"in[- ]?transit|departed|arrived|linehaul|en ?route|terminal|dispatch", t):
        return "IN_TRANSIT"
    if re.search(r"pick(ed)?[- ]?up|picked", t):
        return "PICKED_UP"
    if re.search(r"book|created|label|manifest|pending", t):
        return "BOOKED"
    return "UNKNOWN"


# --------------------------------------------------------------------------------------- stage 1
def normalize(spec: CarrierSpec, raw: dict, ref: str, ref_type: str, account: CarrierAccount) -> TrackingResult:
    m, notes = spec.mapping, []
    vals: dict[str, Any] = {f: get_path(raw, p) for f, p in m.fields.items()}
    for field, template in (m.compose or {}).items():
        composed = compose_value(raw, template)
        if composed not in (None, ""):
            vals[field] = composed
            notes.append(f"compose:{field}")
    if vals.get("pod_datetime"):
        vals["pod_datetime"] = parse_loose_datetime(vals["pod_datetime"]) or vals["pod_datetime"]
    if m.auto:
        for f, (p, v) in auto_fields(raw).items():
            if vals.get(f) in (None, ""):
                vals[f] = v
                notes.append(f"auto:{f}<-{p}")
    events: list[TrackingEvent] = []
    if m.events and m.events.get("path"):
        flds = m.events.get("fields", {})
        compose_ev = m.events.get("compose") or {}
        for it in (get_path(raw, m.events["path"], []) or []):
            data = {f: get_path(it, p) for f, p in flds.items() if f and p}
            for field, template in compose_ev.items():
                composed = compose_value(it, template)
                if composed not in (None, ""):
                    data[field] = composed
            if data.get("timestamp"):
                data["timestamp"] = parse_loose_datetime(data["timestamp"]) or data["timestamp"]
            events.append(TrackingEvent(**data))
    elif m.auto and (found := auto_events(raw)):
        events = found[1]
        notes.append(f"auto:events<-{found[0]}")
    raw_ref = vals.pop("reference", None)
    reference = str(raw_ref) if raw_ref else ref
    raw_status = vals.pop("status", None)
    status = status_from_text(str(raw_status), m.status_map) if raw_status else "UNKNOWN"
    if status == "UNKNOWN" and events:
        for e in (events[0], events[-1]):
            s = status_from_text(" ".join(x for x in (e.status, e.description) if x), m.status_map)
            if s != "UNKNOWN":
                status = s
                notes.append("status<-events")
                break
    return TrackingResult(reference=reference, ref_type=ref_type, carrier_code=spec.code, bill_to=account.bill_to,
                          status=status, raw_status=str(raw_status) if raw_status else None,
                          delivered=status == "DELIVERED", events=events, raw=raw, notes=notes, **vals)


# --------------------------------------------------------------------------------------- stage 2
def apply_map(data: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """Apply a YAML map of output_key -> dotted path or `=literal`."""
    payload: dict[str, Any] = {}
    for key, path in (mapping or {}).items():
        if not key or path is None or path == "":
            continue
        payload[key] = path[1:] if str(path).startswith("=") else get_path(data, str(path))
    return payload


def _load_hook(path: str):
    mod, fn = path.split(":")
    return getattr(importlib.import_module(mod), fn)


def build_payload(spec: CarrierSpec, result: TrackingResult, account: CarrierAccount, run_id: str,
                  meta: dict | None = None) -> dict:
    data = result.model_dump(mode="json", exclude={"raw"})
    data["ctx"] = {"bill_to": account.bill_to, "carrier_code": spec.code, "client": spec.client,
                   "customer_name": account.customer_name, "run_id": run_id, **(meta or {})}
    payload = apply_map(data, spec.payload_map)
    if spec.post_hook:
        payload = _load_hook(spec.post_hook)(payload, result, account) or payload
    return payload
