"""Core data models: backend records, jobs, common tracking schema."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class CarrierAccount(BaseModel):
    """One record from the backend 'carrier config' API (camelCase on the wire).
    Credentials are SecretStr: masked in repr/logs, held in memory only."""
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: int | str
    carrier_name: str = Field(alias="carrierName")
    carrier_code: str = Field(alias="carrierCode")
    tracking_url: str | None = Field(None, alias="trackingUrl")
    bill_to: str = Field(alias="billTo")
    customer_name: str | None = Field(None, alias="customerName")
    processing_frequency: float | None = Field(None, alias="processingFrequency")  # ASSUMED: hours
    processing_delay: float | None = Field(None, alias="processingDelay")          # ASSUMED: seconds between refs
    user_name: str | None = Field(None, alias="userName")
    password: SecretStr | None = None
    is_active: bool = Field(True, alias="isActive")
    total_count: int | None = Field(None, alias="totalCount")
    from_carrier_configuration: bool = Field(False, exclude=True)

    @field_validator("bill_to", "carrier_code", mode="before")
    @classmethod
    def _identifiers_as_strings(cls, value):
        return str(value) if value is not None else value


class PendingRef(BaseModel):
    reference: str            # the PRO / BOL / housebill value
    ref_type: str             # PRO | BOL | ... (the "search by" column)
    meta: dict[str, Any] = {}  # anything else the backend returned for this row (echoed into ctx)


class PendingBatch(BaseModel):
    bill_to: str
    carrier_code: str
    search_by: str
    refs: list[PendingRef]


class TrackingEvent(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)
    timestamp: str | None = None
    description: str | None = None
    location: str | None = None
    status: str | None = None


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*\.?\d*", str(v))
    return float(m.group(0).replace(",", "")) if m else None


class TrackingResult(BaseModel):
    """Common schema every adapter normalizes into."""
    model_config = ConfigDict(coerce_numbers_to_str=True)
    reference: str
    ref_type: str
    carrier_code: str
    bill_to: str
    status: str = "UNKNOWN"   # BOOKED|PICKED_UP|IN_TRANSIT|OUT_FOR_DELIVERY|DELIVERED|EXCEPTION|UNKNOWN
    raw_status: str | None = None
    delivered: bool = False
    pickup_date: str | None = None
    delivery_date: str | None = None
    eta: str | None = None
    origin: str | None = None
    destination: str | None = None
    pieces: int | None = None
    weight: float | None = None
    events: list[TrackingEvent] = []
    scraped_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    raw: dict[str, Any] = {}
    notes: list[str] = []      # what was auto-detected / inferred (visible in probe + logs, not in payload)

    @field_validator("weight", mode="before")
    @classmethod
    def _w(cls, v):
        return _num(v)

    @field_validator("pieces", mode="before")
    @classmethod
    def _p(cls, v):
        n = _num(v)
        return int(n) if n is not None else None
