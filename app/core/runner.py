"""Orchestrator only: fetch accounts, resolve specs, start jobs, report. Business logic lives in job.py."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from app.core.job import AccountJob, CarrierGuard, RunContext, Services, label
from app.core.metrics import RunMetrics
from app.core.resilience import CircuitBreaker, Pacer, retry
from app.logging_setup import carrier_var, client_var, log, register_secret, run_id_var
from app.models import CarrierAccount
from app.settings import CarrierSpec, SpecResolver

logger = logging.getLogger(__name__)


class Runner:
    def __init__(self, sv: Services, resolver: SpecResolver):
        self.sv, self.resolver = sv, resolver
        self._guards: dict[str, CarrierGuard] = {}

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
        ctx = RunContext(run_id=uuid.uuid4().hex[:12], metrics=RunMetrics())
        run_id_var.set(ctx.run_id)
        t0, m = time.time(), ctx.metrics
        accounts = await retry(self.sv.backend.get_carrier_accounts, attempts=3, base=2, cap=30,
                               retry_on=(Exception,))
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
