"""Base interface for reusable carrier implementations."""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.browser.base import BrowserProvider
from app.core.scraper import BatchScraper, ScrapeBatchResult
from app.models import TrackingJob
from app.settings import CarrierSpec


class BaseCarrierAdapter(ABC):
    """Carrier behavior is reusable across clients; account context is per job."""
    def __init__(self, provider: BrowserProvider, spec: CarrierSpec, debug_dir: str):
        self.provider = provider
        self.spec = spec
        self.debug_dir = debug_dir

    @abstractmethod
    async def track_batch(self, jobs: list[TrackingJob]) -> ScrapeBatchResult:
        """Scrape a batch using this carrier's browser workflow."""


class ConfiguredCarrierAdapter(BaseCarrierAdapter):
    """Default adapter for carriers whose interaction is defined by their profile."""
    async def track_batch(self, jobs: list[TrackingJob]) -> ScrapeBatchResult:
        return await BatchScraper(self.provider, self.spec, self.debug_dir).run_batch(jobs, self.spec)
