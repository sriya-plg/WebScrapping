"""Build and POST Pegasus AddCarrierActivity payloads (one request per housebill)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.mapper import normalize
from app.core.scraper import ScrapeBatchResult
from app.logging_setup import log
from app.models import CarrierAccount, CarrierConfig, TrackingJob, TrackingResult
from app.settings import CarrierSpec

import httpx

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
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = fallback or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _activity_events(result: TrackingResult | None) -> list[dict[str, Any]]:
    if not result:
        return []
    out: list[dict[str, Any]] = []
    for event in result.events:
        out.append({
            "code": (event.status or "EVENT")[:64],
            "description": event.description or "",
            "eventDateTime": pegasus_datetime(
                event.timestamp or result.scraped_at, fallback=datetime.now(timezone.utc)),
            "timezone": "UTC",
            "lat": 0,
            "long": 0,
        })
    return out


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
) -> dict[str, Any]:
    bill_to = job.carrier.bill_to
    try:
        bill_to = int(bill_to)
    except (TypeError, ValueError):
        pass
    pod_name = ""
    pod_dt = ""
    if result and result.delivered:
        pod_dt = result.delivery_date or result.scraped_at
        for event in reversed(result.events):
            if event.description:
                pod_name = event.description[:256]
                if event.timestamp:
                    pod_dt = event.timestamp
                break
    pod_fallback = datetime.now(timezone.utc)
    return {
        "housebill": str(job.shipment.housebill or ""),
        "proNumber": str(job.shipment.pro_number or job.search_value or ""),
        "carrierCode": str(job.carrier.carrier_code or ""),
        "serviceLevel": "",
        "billTo": bill_to,
        "mlStatus": ml_status,
        "mlLog": (ml_log or "")[:2000],
        "mlStartDateTime": pegasus_datetime(ml_start),
        "mlEndDateTime": pegasus_datetime(ml_end),
        "mlProcessTime": max(0, int((ml_end - ml_start).total_seconds() * 1000)),
        "mlError": (ml_error or "")[:2000],
        "reference": (result.reference if result else job.search_value) or "",
        "podName": pod_name,
        "podDateTime": pegasus_datetime(pod_dt or None, fallback=ml_end),
        "events": _activity_events(result),
    }


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
            ml_log=ml_log, ml_error=ml_error,
        )

        log(logger, logging.INFO, "AddCarrierActivity prepared",
            housebill=job.shipment.housebill, pro=job.search_value,
            ml_status=ml_status, scrape_outcome=scrape_outcome, outcome=outcome_type,
            event_count=len(payload["events"]))

        if settings.dry_run:
            stats["posted"] += 1
            continue

        try:
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    await post(payload)
                    last_exc = None
                    break
                except httpx.HTTPStatusError as exc:
                    last_exc = exc
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
            stats["posted"] += 1
            log(logger, logging.INFO, "AddCarrierActivity posted",
                housebill=job.shipment.housebill, pro=job.search_value, ml_status=ml_status)
        except Exception as exc:
            stats["failed"] += 1
            log(logger, logging.ERROR, "AddCarrierActivity failed",
                housebill=job.shipment.housebill, pro=job.search_value,
                error=f"{type(exc).__name__}: {exc}")

    if settings.dry_run:
        log(logger, logging.INFO, "dry-run: would post AddCarrierActivity", count=stats["posted"])
    return stats
