"""Immutable runtime context for one backend-owned client/carrier account job."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class JobContext:
    """Identity/configuration carried through scraping and payload construction.

    No client configuration is stored locally. Values are built from each backend
    CarrierConfig record, so two clients using the same carrier remain isolated.
    """

    client_id: str
    client_name: str
    account_id: str
    bill_to: str
    carrier_code: str
    tracking_url: str | None = None
    processing_frequency: float | None = None
    processing_delay: float | None = None

    @classmethod
    def from_carrier_config(cls, config: Any) -> "JobContext":
        bill_to = str(getattr(config, "bill_to", "") or "")
        client_name = str(getattr(config, "customer_name", None) or bill_to or "default")
        return cls(
            client_id=client_name,
            client_name=client_name,
            account_id=str(getattr(config, "id", "") or ""),
            bill_to=bill_to,
            carrier_code=str(getattr(config, "carrier_code", "") or "").strip().upper(),
            tracking_url=getattr(config, "tracking_url", None),
            processing_frequency=getattr(config, "processing_frequency", None),
            processing_delay=getattr(config, "processing_delay", None),
        )
