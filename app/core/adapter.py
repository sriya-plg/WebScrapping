"""Adapter contract. A new carrier = one YAML + one subclass registered with @register."""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from app.browser.base import BrowserProvider, BrowserSession
from app.core.postprocess import normalize_with_config
from app.models import CarrierAccount, TrackingResult
from app.settings import CarrierSpec


class CarrierAdapter(ABC):
    def __init__(self, spec: CarrierSpec, account: CarrierAccount, browser: BrowserProvider | None):
        self.spec, self.account, self.browser = spec, account, browser
        self.cfg = spec.adapter
        self.session: BrowserSession | None = None
        self.io_lock = asyncio.Lock()   # one page per session -> serialize browser interaction
        self.log = logging.getLogger(f"adapter.{spec.code}")

    @property
    def session_key(self) -> str:
        # one cookie profile per client+carrier (each client logs in with its own credentials)
        return f"{self.spec.client}_{self.spec.code}"

    async def authenticate(self) -> None:
        """Open session / log in / clear challenge. Raise BlockedError or ManualInterventionRequired."""

    @abstractmethod
    async def track(self, ref: str, ref_type: str) -> dict:
        """Return the RAW carrier JSON for one reference. Raise NotFoundError / TransientError / BlockedError."""

    def normalize(self, raw: dict, ref: str, ref_type: str) -> TrackingResult:
        """Default: fully config-driven (spec.normalize). Override for odd carriers."""
        return normalize_with_config(self.spec, raw, ref, ref_type, self.account)

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None
