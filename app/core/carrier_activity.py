"""Build and POST Pegasus AddCarrierActivity payloads (one request per housebill)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.mapper import apply_map, get_path, normalize
from app.core.scrape_log import log_scrape_and_tms
from app.core.scraper import ScrapeBatchResult
from app.logging_setup import log
from app.models import CarrierAccount, CarrierConfig, TrackingJob, TrackingResult
from app.settings import ActivityConfig, CarrierSpec

logger = logging.getLogger(__name__)

# Internal scrape outcomes (never send these as mlStatus unless mapped in client activity config).
SCRAPE_OK = "ok"
SCRAPE_NOT_FOUND = "not_found"
SCRAPE_NO_RESPONSE = "no_response"
SCRAPE_MAP_FAILED = "map_failed"

# Pegasus plgtst (2026-10): mlStatus 2, 3, 4 accepted; 0 and 1 return 400.


def pegasus_ml_status(spec: CarrierSpec, scrape_outcome: str) -> int:
    """Map scrape outcome -> Pegasus mlStatus id (API lookup table, not our internal codes)."""
    activity = spec.activity
    if scrape_outcome in activity.ml_status_by_outcome:
        return int(activity.ml_status_by_outcome[scrape_outcome])
    return int(activity.ml_status_default)


def account_from_config(config: CarrierConfig) -> CarrierAccount:
    password = config.password.get_secret_value() if config.password else None
    return CarrierAccount(
        id=config.id,
        carrierName=config.carrier_name,
        carrierCode=config.carrier_code,
        trackingUrl=config.tracking_url,
        billTo=str(config.bill_to),
        customerName=config.customer_name,
        processingFrequency=config.processing_frequency,
        processingDelay=config.processing_delay,
        userName=config.user_name,
        password=password,
        isActive=config.is_active,
        searchBy=config.search_by,
    )


def pegasus_datetime(value: datetime | str | None, *, fallback: datetime | None = None) -> str:
    """Pegasus AddCarrierActivity: yyyy-mm-ddTHH:MM:SSZ (UTC, no fractional seconds)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        dt = fallback or datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        # If text is a time with numeric timezone offset (e.g. '15:18:00-05:00'), strip the offset
        m_time = re.match(r"^(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*[+-]\d{2}:?\d{2}$", text)
        if m_time:
            text = m_time.group(1)
        # If text is a time-only string (HH:MM:SS or HH:MM), combine with fallback date
        if re.match(r"^\d{1,2}:\d{2}(?::\d{2})?$", text):
            if len(text) == 5:
                text = f"{text}:00"
            fb_date = (fallback or datetime.now(timezone.utc)).date()
            text = f"{fb_date.isoformat()}T{text}"
        text = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = fallback or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_STRING_PAYLOAD_KEYS = {
    "housebill", "proNumber", "carrierCode", "serviceLevel", "mlLog", "mlError",
    "reference", "podName", "mlStartDateTime", "mlEndDateTime", "podDateTime",
}
_DATETIME_PAYLOAD_KEYS = {"mlStartDateTime", "mlEndDateTime", "podDateTime"}


def _pod_from_result(result: TrackingResult | None) -> tuple[str, str]:
    if not result:
        return "", ""
    pod_name = (result.pod_name or "").strip()
    pod_dt = (result.pod_datetime or "").strip()
    if result.delivered:
        if not pod_dt:
            pod_dt = result.delivery_date or result.scraped_at or ""
        if not pod_name:
            for event in reversed(result.events):
                if event.description:
                    pod_name = event.description[:256]
                    if not pod_dt and event.timestamp:
                        pod_dt = event.timestamp
                    break
    return pod_name, pod_dt


def _activity_events(
    result: TrackingResult | None,
    events_map: dict[str, str],
    *,
    fallback: datetime,
    preserve_offsets: bool = False,
) -> list[dict[str, Any]]:
    if not result:
        return []
    out: list[dict[str, Any]] = []
    for event in result.events:
        item = apply_map(event.model_dump(mode="json"), events_map)
        code = item.get("code") or event.status or "EVENT"
        # plgtst returns HTTP 500 when event code is longer than 50 characters.
        item["code"] = str(code)[:50]
        item["description"] = item.get("description") or ""
        event_time = item.get("eventDateTime") or event.timestamp or result.scraped_at
        if preserve_offsets and isinstance(event_time, str):
            item["eventDateTime"] = event_time
        else:
            item["eventDateTime"] = pegasus_datetime(event_time, fallback=fallback)
        if item.get("timezone"):
            item["timezone"] = str(item["timezone"]).strip()
        else:
            item.pop("timezone", None)  # no timezone from carrier: omit the key
        if item.get("lat") in (None, ""):
            item["lat"] = 0
        if item.get("long") in (None, ""):
            item["long"] = 0
        out.append(item)
    return out


def _activity_ctx(
    job: TrackingJob,
    result: TrackingResult | None,
    *,
    ml_start: datetime,
    ml_end: datetime,
    ml_status: int,
    ml_log: str,
    ml_error: str,
    preserve_pod_datetime: bool = False,
) -> dict[str, Any]:
    bill_to = job.carrier.bill_to
    try:
        bill_to = int(bill_to)
    except (TypeError, ValueError):
        pass
    pod_name, pod_dt = _pod_from_result(result)
    return {
        "housebill": str(job.shipment.housebill or ""),
        "pro_number": str(job.shipment.pro_number or job.search_value or ""),
        "carrier_code": str(job.carrier.carrier_code or ""),
        "bill_to": bill_to,
        "ml_status": ml_status,
        "ml_log": (ml_log or "")[:2000],
        "ml_start": pegasus_datetime(ml_start),
        "ml_end": pegasus_datetime(ml_end),
        "ml_process_time": max(0, int((ml_end - ml_start).total_seconds() * 1000)),
        "ml_error": (ml_error or "")[:2000],
        "service_level": (result.service_level if result else None) or "",
        "pod_name": pod_name,
        "pod_datetime": (
            pod_dt if preserve_pod_datetime and pod_dt
            else pegasus_datetime(pod_dt or None, fallback=ml_end)
        ),
        "reference": (result.reference if result else job.search_value) or "",
    }


def build_add_carrier_activity(
    job: TrackingJob,
    result: TrackingResult | None,
    *,
    outcome: str,
    summary: dict[str, Any],
    ml_start: datetime,
    ml_end: datetime,
    ml_status: int,
    ml_log: str,
    ml_error: str = "",
    spec: CarrierSpec | None = None,
) -> dict[str, Any]:
    """Build the AddCarrierActivity body from YAML `activity.payload_map` / `events_map`.

    Runtime scrape fields (mlStatus, timestamps, housebill, ...) live on `ctx.*`.
    Carrier-mapped fields (service_level, pod_*, events) come from TrackingResult.
    Change paths in client.yaml or the carrier mapping.yaml — not this function.
    """
    activity = spec.activity if spec is not None else ActivityConfig()
    ctx = _activity_ctx(
        job, result, ml_start=ml_start, ml_end=ml_end,
        ml_status=ml_status, ml_log=ml_log, ml_error=ml_error,
        preserve_pod_datetime=activity.preserve_pod_datetime,
    )
    data: dict[str, Any] = dict(ctx)
    if result:
        dumped = result.model_dump(mode="json", exclude={"raw"})
        dumped.pop("events", None)
        data.update(dumped)
    # TrackingResult.bill_to / carrier_code must not replace intake identity (numeric billTo, vendor code).
    data.update(ctx)
    data["ctx"] = ctx
    data["service_level"] = get_path(data, "service_level") or ctx["service_level"]
    data["pod_name"] = get_path(data, "pod_name") or ctx["pod_name"]
    data["pod_datetime"] = get_path(data, "pod_datetime") or ctx["pod_datetime"]
    data["reference"] = get_path(data, "reference") or ctx["reference"]

    payload = apply_map(data, activity.payload_map)
    if payload.get("billTo") in (None, ""):
        payload["billTo"] = ctx["bill_to"]
    else:
        try:
            payload["billTo"] = int(payload["billTo"])
        except (TypeError, ValueError):
            payload["billTo"] = ctx["bill_to"]
    if not payload.get("carrierCode"):
        payload["carrierCode"] = ctx["carrier_code"]
    if not payload.get("housebill"):
        payload["housebill"] = ctx["housebill"]
    if not payload.get("proNumber"):
        payload["proNumber"] = ctx["pro_number"]
    for key in _STRING_PAYLOAD_KEYS:
        if key in payload and payload[key] is None:
            payload[key] = ""
    for key in _DATETIME_PAYLOAD_KEYS:
        if key in payload:
            if key == "podDateTime" and activity.preserve_pod_datetime:
                continue
            payload[key] = pegasus_datetime(payload[key] or None, fallback=ml_end)
    if "mlLog" in payload:
        payload["mlLog"] = str(payload["mlLog"] or "")[:2000]
    if "mlError" in payload:
        payload["mlError"] = str(payload["mlError"] or "")[:2000]
    payload["events"] = _activity_events(
        result, activity.events_map, fallback=ml_end,
        preserve_offsets=activity.preserve_event_offsets,
    )
    return payload


def _unpack_activity_result(api_result: Any) -> tuple[bool, int | None, Any]:
    """Normalize add_carrier_activity() return values (dict, None, or other)."""
    if api_result is None:
        return True, None, ""
    if isinstance(api_result, dict) and ("ok" in api_result or "status_code" in api_result or "body" in api_result):
        body = api_result.get("body", "")
        status_code = api_result.get("status_code")
        ok = bool(api_result.get("ok", True))
        if ok:
            reject = _activity_body_rejected(body)
            if reject:
                return False, status_code, body
        return ok, status_code, body
    return True, None, api_result


def _activity_body_rejected(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    if body.get("isSuccess") is False or body.get("success") is False or body.get("isError") is True:
        return str(body.get("error") or body.get("errorMessage") or body.get("message") or body)[:500]
    return ""


def _log_activity_http_error(job: TrackingJob, payload: dict[str, Any], exc: httpx.HTTPStatusError) -> None:
    response = exc.response
    body: Any = ""
    if response is not None:
        try:
            body = response.json()
        except ValueError:
            body = (response.text or "")[:2000]
        logger.info(
            "--- TMS HTTP response (AddCarrierActivity) housebill=%s pro=%s http=%s ---\n%s\n--- end TMS HTTP response ---",
            payload.get("housebill"), payload.get("proNumber"),
            response.status_code,
            json.dumps(body, indent=2, default=str, ensure_ascii=False) if not isinstance(body, str) else body,
        )
        log(logger, logging.WARNING, "AddCarrierActivity HTTP error",
            housebill=job.shipment.housebill, pro=job.search_value,
            http_status=response.status_code, response=str(body)[:500])


def _normalize_raw(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"data": raw}
    return {"value": raw}


async def post_scrape_activities(
    settings,
    backend,
    jobs: list[TrackingJob],
    spec: CarrierSpec,
    run_id: str,
    outcome: ScrapeBatchResult,
) -> dict[str, int]:
    """POST AddCarrierActivity once per intake job (housebill row)."""
    account = account_from_config(jobs[0].carrier) if jobs else None
    if account is None:
        return {"posted": 0, "skipped": 0, "failed": 0}

    stats = {"posted": 0, "skipped": 0, "failed": 0}
    batch_start = datetime.now(timezone.utc)
    post = getattr(backend, "add_carrier_activity", None)
    if post is None and not settings.dry_run:
        log(logger, logging.WARNING, "backend does not implement add_carrier_activity")
        stats["skipped"] = len(jobs)
        return stats

    for job in jobs:
        ml_end = datetime.now(timezone.utc)
        raw = outcome.received.get(job.search_value)
        outcome_type = outcome.outcomes.get(job.search_value, "missing")
        summary = outcome.summaries.get(job.search_value, {})

        result: TrackingResult | None = None
        scrape_outcome = SCRAPE_OK
        ml_log = "tracking scrape completed"
        ml_error = ""

        if raw is None:
            scrape_outcome = SCRAPE_NO_RESPONSE
            ml_log = f"outcome=missing; no carrier JSON captured for PRO {job.search_value}"
            if any(e.get("pro") == job.search_value or job.search_value in (e.get("pros") or "")
                   for e in outcome.errors):
                ml_error = next((e.get("message", "") for e in outcome.errors
                                 if e.get("pro") == job.search_value), "scrape error")
        elif outcome_type == "not_found":
            scrape_outcome = SCRAPE_NOT_FOUND
            ml_log = summary.get("error_message") or "carrier reported shipment not found"
            ml_error = ml_log
            ml_log = f"outcome=not_found; {ml_log}"
            try:
                result = normalize(spec, _normalize_raw(raw), job.search_value, "PRO", account)
            except Exception as exc:
                scrape_outcome = SCRAPE_MAP_FAILED
                ml_log = "mapping failed for not-found payload"
                ml_error = f"{type(exc).__name__}: {exc}"
        else:
            try:
                result = normalize(spec, _normalize_raw(raw), job.search_value, "PRO", account)
                ml_log = f"outcome={outcome_type}; status={result.status}"
            except Exception as exc:
                scrape_outcome = SCRAPE_MAP_FAILED
                ml_log = "mapping failed"
                ml_error = f"{type(exc).__name__}: {exc}"
                log(logger, logging.WARNING, "AddCarrierActivity mapping failed",
                    housebill=job.shipment.housebill, pro=job.search_value, error=ml_error)

        ml_status = pegasus_ml_status(spec, scrape_outcome)
        payload = build_add_carrier_activity(
            job, result, outcome=outcome_type, summary=summary,
            ml_start=batch_start, ml_end=ml_end, ml_status=ml_status,
            ml_log=ml_log, ml_error=ml_error, spec=spec,
        )

        activity = spec.activity if spec is not None else ActivityConfig()
        log_scrape_and_tms(
            housebill=str(payload.get("housebill") or job.shipment.housebill or ""),
            pro=str(payload.get("proNumber") or job.search_value or ""),
            scrape_outcome=scrape_outcome,
            raw=raw,
            result=result,
            payload=payload,
            payload_map=activity.payload_map,
        )

        if settings.dry_run:
            stats["posted"] += 1
            continue

        try:
            last_exc: Exception | None = None
            api_result: dict[str, Any] | None = None
            for attempt in range(3):
                try:
                    api_result = await post(payload)
                    last_exc = None
                    break
                except httpx.HTTPStatusError as exc:
                    last_exc = exc
                    _log_activity_http_error(job, payload, exc)
                    if exc.response.status_code < 500:
                        break
                    if attempt + 1 < 3:
                        await asyncio.sleep(min(15, 2 ** attempt))
                except Exception as exc:
                    last_exc = exc
                    if attempt + 1 < 3:
                        await asyncio.sleep(min(15, 2 ** attempt))
            if last_exc:
                raise last_exc
            ok, status_code, body = _unpack_activity_result(api_result)
            logger.info(
                "--- TMS HTTP response (AddCarrierActivity) housebill=%s pro=%s http=%s ---\n%s\n--- end TMS HTTP response ---",
                payload.get("housebill"), payload.get("proNumber"),
                status_code if status_code is not None else "n/a",
                json.dumps(body, indent=2, default=str, ensure_ascii=False) if not isinstance(body, str) else body,
            )
            if not ok:
                stats["failed"] += 1
                log(logger, logging.ERROR, "AddCarrierActivity rejected",
                    housebill=job.shipment.housebill, pro=job.search_value,
                    http_status=status_code, response=str(body)[:500])
                continue
            stats["posted"] += 1
            log(logger, logging.INFO, "AddCarrierActivity posted",
                housebill=job.shipment.housebill, pro=job.search_value,
                bill_to=payload.get("billTo"), ml_status=ml_status,
                http_status=status_code)
        except Exception as exc:
            stats["failed"] += 1
            log(logger, logging.ERROR, "AddCarrierActivity failed",
                housebill=job.shipment.housebill, pro=job.search_value,
                error=f"{type(exc).__name__}: {exc}")

    if settings.dry_run:
        log(logger, logging.INFO, "dry-run: would post AddCarrierActivity", count=stats["posted"])
    return stats
