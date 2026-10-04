"""Shared output schema -- every carrier's normalizer produces one of these,
so nothing downstream (storage, API, UI) needs to know which carrier or
which scraping method produced a given result."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrackingJob:
    """One unit of work, as it will arrive from the backend API.

    Credentials travel with the job and are used only in-memory for the
    login step -- never logged, never written to disk. See
    GenericScraper._run_single_attempt, which explicitly redacts
    `password` before any log call touches job data.
    """
    bill_to_customer_code: str      # which customer this lookup is for
    carrier: str                    # "estes" | "saia" | "forward_air" (must match a registered carrier)
    url: str                        # tracking page URL to use for this job (overrides carrier YAML default if set)
    username: str
    password: str
    reference_type: str             # "bol" | "pro" | "po" -- which field/column to search by
    reference_number: str

    @classmethod
    def from_dict(cls, d: dict) -> "TrackingJob":
        required = [
            "bill_to_customer_code", "carrier", "url",
            "username", "password", "reference_type", "reference_number",
        ]
        missing = [f for f in required if f not in d]
        if missing:
            raise ValueError(f"TrackingJob payload missing required fields: {missing}")
        return cls(**{k: d[k] for k in required})


@dataclass
class TrackingEvent:
    date: str
    status: str
    description: str
    location: Optional[str] = None


@dataclass
class TrackingResult:
    carrier: str
    reference: str
    bill_to_customer_code: Optional[str] = None
    status: Optional[str] = None
    status_detail: Optional[str] = None
    pickup_date: Optional[str] = None
    delivery_date: Optional[str] = None
    pieces: Optional[int] = None
    weight_lbs: Optional[float] = None
    shipper: Optional[str] = None
    consignee: Optional[str] = None
    history: list[TrackingEvent] = field(default_factory=list)
    source: str = "scrape"
    raw: Optional[dict] = None

