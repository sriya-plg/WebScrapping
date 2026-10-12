"""Human-readable scrape capture logging (raw network JSON + mapped fields)."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.logging_setup import log, run_id_var
from app.models import TrackingResult

logger = logging.getLogger(__name__)


def json_preview(value: Any, max_chars: int = 2000) -> str:
    try:
        text = json.dumps(value, default=str, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n... (+{len(text) - max_chars} chars truncated)"


def extracted_summary(result: TrackingResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    return {
        "status": result.status,
        "raw_status": result.raw_status,
        "delivered": result.delivered,
        "origin": result.origin,
        "destination": result.destination,
        "pickup_date": result.pickup_date,
        "delivery_date": result.delivery_date,
        "eta": result.eta,
        "pieces": result.pieces,
        "weight": result.weight,
        "service_level": result.service_level,
        "pod_name": result.pod_name,
        "pod_datetime": result.pod_datetime,
        "events": len(result.events),
    }


def _safe_filename(pro: str) -> str:
    return re.sub(r"[^\w.-]+", "_", pro)[:80] or "unknown"


def save_capture_file(
    debug_dir: Path,
    carrier_code: str,
    pro: str,
    raw: Any,
    *,
    response_url: str | None = None,
) -> Path | None:
    if raw is None:
        return None
    run_id = run_id_var.get()
    if not run_id or run_id == "-":
        run_id = "norun"
    folder = debug_dir / "captures" / run_id / carrier_code
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_safe_filename(pro)}.json"
    payload: dict[str, Any] = {"pro": pro, "response_url": response_url, "raw": raw}
    path.write_text(json.dumps(payload, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def log_raw_capture(
    *,
    pro: str,
    raw: Any,
    outcome: str,
    response_url: str | None = None,
    summary: dict[str, Any] | None = None,
    max_chars: int = 2000,
    capture_path: Path | None = None,
) -> None:
    preview = json_preview(raw, max_chars=max_chars) if raw is not None else "(no JSON captured)"
    log(
        logger,
        logging.INFO,
        "scrape raw network JSON",
        pro=pro,
        outcome=outcome,
        response_url=(response_url or "")[:200],
        capture_file=str(capture_path) if capture_path else "",
        error_code=(summary or {}).get("error_code"),
        error_message=(summary or {}).get("error_message"),
        data_count=(summary or {}).get("data_count"),
    )
    logger.info("--- raw JSON pro=%s ---\n%s\n--- end raw JSON ---", pro, preview)


def log_extracted_fields(
    *,
    pro: str,
    housebill: str,
    scrape_outcome: str,
    raw: Any,
    result: TrackingResult | None,
    max_chars: int = 2000,
) -> None:
    log(
        logger,
        logging.INFO,
        "scrape extracted fields",
        pro=pro,
        housebill=housebill,
        scrape_outcome=scrape_outcome,
        mapped=extracted_summary(result),
    )
    if raw is not None:
        logger.info(
            "--- mapped from raw pro=%s housebill=%s ---\n%s\n--- end mapped raw ---",
            pro,
            housebill,
            json_preview(raw, max_chars=max_chars),
        )
    else:
        logger.info("--- no carrier JSON captured for pro=%s housebill=%s ---", pro, housebill)
    if raw is None and result is not None:
        logger.info(
            "--- normalized result pro=%s ---\n%s\n--- end normalized ---",
            pro,
            json_preview(result.model_dump(exclude={"raw"}), max_chars=max_chars),
        )


def log_scrape_and_tms(
    *,
    housebill: str,
    pro: str,
    scrape_outcome: str,
    raw: Any,
    result: TrackingResult | None,
    payload: dict[str, Any],
    payload_map: dict[str, str] | None = None,
    scrape_max_chars: int = 20000,
) -> None:
    """Log carrier scrape JSON, then the exact AddCarrierActivity body from payload_map."""
    log(
        logger,
        logging.INFO,
        "scrape capture + TMS payload",
        housebill=housebill,
        pro=pro,
        scrape_outcome=scrape_outcome,
        bill_to=payload.get("billTo"),
        carrier_code=payload.get("carrierCode"),
        mapped=extracted_summary(result),
        event_count=len(payload.get("events") or []),
        payload_keys=list(payload.keys()),
        payload_map_keys=list(payload_map or {}),
    )
    scrape_text = json_preview(raw, max_chars=scrape_max_chars) if raw is not None else "(no JSON captured)"
    logger.info(
        "--- scrape capture housebill=%s pro=%s ---\n%s\n--- end scrape capture ---",
        housebill, pro, scrape_text,
    )
    logger.info(
        "--- TMS payload (AddCarrierActivity / payload_map) housebill=%s pro=%s ---\n%s\n--- end TMS payload ---",
        housebill, pro, json.dumps(payload, indent=2, default=str, ensure_ascii=False),
    )
