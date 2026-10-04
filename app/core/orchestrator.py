"""
Single entry point across every registered carrier:

    from app.core.orchestrator import TrackingOrchestrator
    orch = TrackingOrchestrator()
    result = orch.track("estes", "1848665720")                    # manual/simple lookup
    results = orch.track_batch("saia", ["77133675090", ...])
    all_results = orch.track_all({"estes": [...], "saia": [...]})

    # backend-driven, job-based (real flow once the backend API exists):
    result = orch.track_job(job)                                  # single TrackingJob
    results = orch.track_jobs(jobs)                                # mixed-carrier list, grouped automatically
"""

from collections import defaultdict

from app.carriers.registry import get_scraper, registered_carriers
from app.core.exceptions import ScraperBlockedError
from app.core.logger import get_logger
from app.core.models import TrackingJob

logger = get_logger("orchestrator")


class TrackingOrchestrator:
    def track(self, carrier: str, reference: str):
        return get_scraper(carrier).track(reference)

    def track_batch(self, carrier: str, references: list[str]):
        return get_scraper(carrier).track_many(references)

    def track_all(self, references_by_carrier: dict[str, list[str]]) -> dict:
        """Runs carriers sequentially (not in parallel) -- consistent with
        the within-carrier pacing rationale: no simultaneous load spikes
        across sites either."""
        all_results = {}
        for carrier, refs in references_by_carrier.items():
            logger.info("orchestrator_carrier_start", extra={"carrier": carrier, "count": len(refs)})
            try:
                all_results[carrier] = self.track_batch(carrier, refs)
            except ScraperBlockedError:
                logger.error("orchestrator_carrier_blocked", extra={"carrier": carrier})
                all_results[carrier] = []
        return all_results

    def track_job(self, job: TrackingJob):
        return get_scraper(job.carrier).track_job(job)

    def track_jobs(self, jobs: list[TrackingJob]) -> dict:
        """Groups a mixed-carrier job list by carrier, then runs each
        carrier's jobs sequentially through that carrier's scraper --
        same "no simultaneous spikes" rationale as track_all."""
        by_carrier: dict[str, list[TrackingJob]] = defaultdict(list)
        for job in jobs:
            by_carrier[job.carrier].append(job)

        all_results = {}
        for carrier, carrier_jobs in by_carrier.items():
            logger.info("orchestrator_job_batch_start", extra={"carrier": carrier, "count": len(carrier_jobs)})
            try:
                all_results[carrier] = get_scraper(carrier).track_jobs(carrier_jobs)
            except ScraperBlockedError:
                logger.error("orchestrator_job_batch_blocked", extra={"carrier": carrier})
                all_results[carrier] = []
        return all_results

    def available_carriers(self) -> list[str]:
        return registered_carriers()

