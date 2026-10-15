"""Orchestrator only: fetch accounts, resolve specs, start jobs, report. Business logic lives in job.py."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from app.backend.client import BackendUnavailable
from app.core.intake import build_jobs
from app.core.job import AccountJob, CarrierGuard, RunContext, Services, label
from app.core.metrics import RunMetrics
from app.core.resilience import CircuitBreaker, Pacer, retry
from app.core.batch_updates import enqueue_tracking_updates
from app.core.scraper import BatchScraper
from app.logging_setup import carrier_var, client_var, log, register_secret, run_file_logger, run_id_var
from app.models import CarrierAccount
from app.settings import CarrierSpec, SpecResolver

logger = logging.getLogger(__name__)


class Runner:
    def __init__(self, sv: Services, resolver: SpecResolver):
        self.sv, self.resolver = sv, resolver
        self._guards: dict[str, CarrierGuard] = {}
        self._run_lock = asyncio.Lock()
        self.last_results: list[dict] = []

    def _guard(self, spec: CarrierSpec, a: CarrierAccount) -> CarrierGuard:
        L = spec.limits
        g = self._guards.setdefault(spec.code, CarrierGuard(Pacer(L.max_concurrency, L.min_delay_s, L.max_delay_s),
                                                            CircuitBreaker(L.breaker_threshold, L.breaker_cooldown_s),
                                                            asyncio.Lock()))
        if a.processing_delay:                        # backend-driven pacing floor
            g.pacer.min_delay = max(L.min_delay_s, a.processing_delay)
            g.pacer.max_delay = max(L.max_delay_s, a.processing_delay * 2)
        return g

    def _due(self, spec: CarrierSpec, a: CarrierAccount) -> bool:
        if not a.processing_frequency:
            return True
        last = float(self.sv.store.kv_get(f"last:{spec.client}:{spec.code}:{a.bill_to}") or 0)
        tol = self.sv.settings.schedule.jitter_minutes * 60    # a jittered tick may arrive early by up to the jitter
        return time.time() - last >= a.processing_frequency * 3600 - tol

    async def run_once(self, only: set[str] | None = None, force: bool = False,
                       only_clients: set[str] | None = None) -> dict:
        async with self._run_lock:
            run_id = uuid.uuid4().hex[:12]
            run_id_var.set(run_id)
            logs_dir = getattr(self.sv.settings, "logs_dir", "logs")
            with run_file_logger(run_id=run_id, logs_dir=logs_dir) as log_file:
                logger.info("run started | run_id=%s log_file=%s", run_id, log_file)
                try:
                    res = await self._run_once(only, force, only_clients)
                    logger.info("run finished | run_id=%s log_file=%s", run_id, log_file)
                    return res
                except Exception as exc:
                    logger.exception("run failed | run_id=%s log_file=%s error=%s", run_id, log_file, exc)
                    raise

    async def _run_once(self, only: set[str] | None = None, force: bool = False,
                        only_clients: set[str] | None = None) -> dict:
        if hasattr(self.sv.backend, "get_carrier_configs") and hasattr(self.sv.backend, "get_shipments"):
            return await self._run_batch_once(only, only_clients)
        rid = run_id_var.get()
        if not rid or rid == "-":
            rid = uuid.uuid4().hex[:12]
            run_id_var.set(rid)
        ctx = RunContext(run_id=rid, metrics=RunMetrics())
        run_id_var.set(ctx.run_id)
        t0, m = time.time(), ctx.metrics
        accounts = await retry(self.sv.backend.get_carrier_accounts, attempts=1, base=2, cap=30,
                               retry_on=(BackendUnavailable,))
        jobs: list[tuple[CarrierSpec, CarrierAccount]] = []
        for a in accounts:
            if a.password:
                register_secret(a.password.get_secret_value())
            if not a.is_active:
                continue
            spec = self.resolver.resolve(a.bill_to, a.carrier_code)
            if spec is None:
                m.count(f"{a.bill_to}/{a.carrier_code}", "not_configured")
                log(logger, logging.WARNING, "carrier not configured for this client (or its config is broken)",
                    bill_to=a.bill_to, carrier_code=a.carrier_code)
                continue
            if (only and spec.code.upper() not in only) or (only_clients and spec.client.upper() not in only_clients):
                continue
            if not force and not self._due(spec, a):
                m.count(label(spec), "skipped_not_due")
                continue
            jobs.append((spec, a))

        async def guarded(spec: CarrierSpec, a: CarrierAccount):
            run_id_var.set(ctx.run_id)
            client_var.set(spec.client)
            carrier_var.set(spec.code)
            await AccountJob(self.sv, ctx, spec, a, self._guard(spec, a)).run()

        try:
            results = await asyncio.gather(*[guarded(s, a) for s, a in jobs], return_exceptions=True)
            for (s, a), r in zip(jobs, results):
                if isinstance(r, BaseException):      # last-resort containment: one job can't sink the run
                    m.count(label(s), "job_crashed")
                    logger.error("job crashed: %s/%s billTo=%s", s.client, s.code, a.bill_to, exc_info=r)
        finally:
            for p in ctx.providers.values():
                try:
                    await p.close()
                except Exception:
                    pass
        summary = m.summary()
        log(logger, logging.INFO, "run complete", run_id=ctx.run_id, seconds=round(time.time() - t0, 1), metrics=summary)
        return summary

    async def _run_batch_once(self, only: set[str] | None, only_clients: set[str] | None) -> dict:
        """Phase 2 path: intake groups -> browser batches -> raw results, isolated per carrier."""
        started = time.time()
        rid = run_id_var.get()
        if not rid or rid == "-":
            rid = uuid.uuid4().hex[:12]
            run_id_var.set(rid)
        grouped = await build_jobs(self.sv.backend)
        groups: dict[tuple[int, str], list] = {}
        for carriers in grouped.values():
            for carrier_code, jobs in carriers.items():
                for job in jobs:
                    groups.setdefault((job.carrier.bill_to, carrier_code), []).append(job)
        self.last_results = []
        summary: dict[str, dict] = {}

        async def scrape_group(key: tuple[int, str], jobs: list):
            bill_to, code = key
            config = jobs[0].carrier
            spec = self.resolver.resolve(str(bill_to), code)
            if spec is None:
                logger.warning("carrier not configured", extra={"fields": {"bill_to": bill_to,
                               "carrier_code": code}})
                return
            if only and spec.code.upper() not in only:
                return
            if only_clients and spec.client.upper() not in only_clients and str(bill_to).upper() not in only_clients:
                return
            client_var.set(spec.client)
            carrier_var.set(spec.code)
            provider = self.sv.provider_factory(spec.browser, self.sv.settings.state_dir)
            try:
                outcome = await BatchScraper(provider, spec, self.sv.settings.debug_dir).run_batch(jobs, spec)
                activity_stats = await enqueue_tracking_updates(
                    self.sv.settings, self.sv.backend, config, spec, run_id_var.get() or "", outcome, jobs)
                self.last_results.append({"client": spec.client, "carrier": spec.code,
                                          "bill_to": bill_to, "result": outcome})
                label_key = f"{spec.client}/{spec.code}"
                found_n = sum(1 for o in outcome.outcomes.values() if o == "found")
                not_found_n = sum(1 for o in outcome.outcomes.values() if o == "not_found")
                summary[label_key] = {
                    "requested": len(outcome.requested),
                    "received": len(outcome.received),
                    "found": found_n,
                    "not_found": not_found_n,
                    "missing": len(outcome.missing),
                    "errors": len(outcome.errors),
                    "activity_posted": activity_stats.get("posted", 0),
                    "activity_failed": activity_stats.get("failed", 0),
                }
                log(logger, logging.INFO, "carrier batch complete", client=spec.client,
                    carrier_code=spec.code, bill_to=bill_to, requested=len(outcome.requested),
                    received=len(outcome.received), found=found_n, not_found=not_found_n,
                    missing=len(outcome.missing), failures=len(outcome.errors))
            finally:
                try:
                    await provider.close()
                except Exception as exc:
                    logger.warning("browser provider close failed", extra={"fields": {
                        "carrier_code": code, "error": type(exc).__name__}})

        outcomes = await asyncio.gather(*(scrape_group(key, jobs) for key, jobs in groups.items()),
                                         return_exceptions=True)
        for (bill_to, code), outcome in zip(groups, outcomes):
            if isinstance(outcome, BaseException):
                summary[f"{bill_to}/{code}"] = {"failures": 1}
                logger.error("carrier batch failed", extra={"fields": {"bill_to": bill_to,
                             "carrier_code": code, "error": type(outcome).__name__}})
        log(logger, logging.INFO, "run complete", seconds=round(time.time() - started, 1),
            carriers_fetched=len(groups), jobs_built=sum(len(j) for j in groups.values()), summary=summary)
        return {**summary, "scrape_results": self.last_results}
