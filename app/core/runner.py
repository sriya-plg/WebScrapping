"""One run = fetch accounts -> per (billTo, carrier) fetch pending -> track -> normalize -> map -> post."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from typing import Callable

from app.backend.client import BackendClient
from app.browser.factory import make_provider
from app.core.intervention import InterventionQueue
from app.core.metrics import RunMetrics
from app.core.postprocess import build_payload
from app.core.registry import get_adapter_class
from app.core.resilience import (BlockedError, CircuitBreaker, ManualInterventionRequired, NotFoundError,
                                 Pacer, retry)
from app.core.state import StateStore
from app.logging_setup import carrier_var, client_var, log, register_secret, run_id_var
from app.models import CarrierAccount, PendingRef
from app.settings import AppSettings, CarrierSpec, SpecResolver


def _mk(spec: CarrierSpec) -> str:
    return f"{spec.client}/{spec.code}"   # metrics/log label: client/carrier

logger = logging.getLogger(__name__)


def idempotency_key(code: str, bill_to: str, ref: str, status: str, last_event: str | None,
                    delivery: str | None) -> str:
    return hashlib.sha1(f"{code}|{bill_to}|{ref}|{status}|{last_event}|{delivery}".encode()).hexdigest()


class Runner:
    def __init__(self, settings: AppSettings, backend: BackendClient, store: StateStore,
                 queue: InterventionQueue, specs: SpecResolver,
                 provider_factory: Callable = make_provider):
        self.s, self.backend, self.store, self.queue, self.specs = settings, backend, store, queue, specs
        self.provider_factory = provider_factory
        self.metrics = RunMetrics()
        self._pacers: dict[str, Pacer] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._providers: dict = {}

    # ---- helpers -------------------------------------------------------------------------------
    def _due(self, a: CarrierAccount) -> bool:
        if not a.processing_frequency:
            return True
        last = float(self.store.kv_get(f"last:{a.carrier_code.upper()}:{a.bill_to}") or 0)
        tol = self.s.schedule.jitter_minutes * 60  # a jittered tick may arrive early by up to the jitter
        return time.time() - last >= a.processing_frequency * 3600 - tol

    def _objs(self, spec: CarrierSpec, a: CarrierAccount):
        L = spec.limits
        p = self._pacers.setdefault(spec.code, Pacer(L.max_concurrency, L.min_delay_s, L.max_delay_s))
        if a.processing_delay:  # backend-driven pacing floor
            p.min_delay = max(L.min_delay_s, a.processing_delay)
            p.max_delay = max(L.max_delay_s, a.processing_delay * 2)
        b = self._breakers.setdefault(spec.code, CircuitBreaker(L.breaker_threshold, L.breaker_cooldown_s))
        return p, b, self._locks.setdefault(spec.code, asyncio.Lock())

    # ---- run -----------------------------------------------------------------------------------
    async def run_once(self, only: set[str] | None = None, force: bool = False,
                       only_clients: set[str] | None = None) -> dict:
        self.metrics = RunMetrics()
        run_id = uuid.uuid4().hex[:12]
        run_id_var.set(run_id)
        t0 = time.time()
        accounts = await retry(self.backend.get_carrier_accounts, attempts=3, base=2, cap=30,
                               retry_on=(Exception,))
        jobs: list[tuple[CarrierSpec, CarrierAccount]] = []
        for a in accounts:
            if a.password:
                register_secret(a.password.get_secret_value())
            if not a.is_active:
                continue
            spec = self.specs.resolve(a.bill_to, a.carrier_code)
            if spec is None:
                self.metrics.count(f"{a.bill_to}/{a.carrier_code}", "not_configured")
                log(logger, logging.WARNING, "carrier not configured for this client", bill_to=a.bill_to,
                    carrier_code=a.carrier_code)
                continue
            if only_clients and spec.client.upper() not in only_clients:
                continue
            if only and spec.code.upper() not in only:
                continue
            if not force and not self._due(a):
                self.metrics.count(_mk(spec), "skipped_not_due")
                continue
            jobs.append((spec, a))
        try:
            results = await asyncio.gather(*[self._run_account(run_id, s, a) for s, a in jobs],
                                           return_exceptions=True)
            for (s, a), r in zip(jobs, results):
                if isinstance(r, Exception):
                    self.metrics.count(_mk(s), "account_failed")
                    logger.error("account run crashed", exc_info=r)
        finally:
            for p in self._providers.values():
                await p.close()
            self._providers.clear()
        summary = self.metrics.summary()
        log(logger, logging.INFO, "run complete", run_id=run_id, seconds=round(time.time() - t0, 1), metrics=summary)
        return summary

    async def _run_account(self, run_id: str, spec: CarrierSpec, a: CarrierAccount) -> None:
        run_id_var.set(run_id)
        carrier_var.set(spec.code)
        client_var.set(spec.client)
        pacer, breaker, lock = self._objs(spec, a)
        async with lock:  # accounts of one carrier run one after another (shared rate limit/breaker)
            m = self.metrics
            if not breaker.allow():
                m.count(_mk(spec), "skipped_circuit_open")
                log(logger, logging.WARNING, "circuit open; skipping", billTo=a.bill_to)
                return
            batch = await retry(lambda: self.backend.get_pending(a, spec), attempts=3, base=2, cap=30,
                                retry_on=(Exception,))
            if not batch.refs:
                self.store.kv_set(f"last:{spec.code}:{a.bill_to}", str(time.time()))
                return
            m.count(_mk(spec), "pending", len(batch.refs))
            provider = None
            pkey = f"{spec.client}:{spec.code}"
            if pkey not in self._providers:
                self._providers[pkey] = self.provider_factory(spec.browser, self.s.state_dir)
            provider = self._providers[pkey]
            adapter = get_adapter_class(spec.code, spec.client)(spec, a, provider)
            all_refs = [r.reference for r in batch.refs]
            try:
                try:
                    await retry(adapter.authenticate, attempts=2, base=3, cap=20)
                except (BlockedError, ManualInterventionRequired) as e:
                    breaker.record_block()
                    m.count(_mk(spec), "blocked", len(all_refs))
                    await self.queue.raise_(run_id, spec.code, a.bill_to, all_refs, str(e),
                                            getattr(e, "url", None), client=spec.client)
                    return
                pending_payloads: list[tuple[str, dict]] = []
                blocked: list[str] = []

                async def one(r: PendingRef):
                    if breaker.is_open:       # tripped mid-batch: stop hitting the site
                        blocked.append(r.reference)
                        return
                    async with pacer:
                        t = time.time()
                        try:
                            raw = await retry(lambda: adapter.track(r.reference, r.ref_type),
                                              attempts=spec.limits.retries, base=spec.limits.backoff_base_s,
                                              cap=spec.limits.backoff_cap_s)
                            res = adapter.normalize(raw, r.reference, r.ref_type)
                            last = res.events[0].timestamp if res.events else None
                            key = idempotency_key(spec.code, a.bill_to, r.reference, res.status, last,
                                                  res.delivery_date)
                            if self.store.was_sent(key):
                                m.count(_mk(spec), "unchanged")
                            else:
                                pending_payloads.append((key, build_payload(spec, res, a, run_id, r.meta)))
                                m.count(_mk(spec), "success")
                            breaker.record_success()
                        except NotFoundError:
                            m.count(_mk(spec), "not_found")
                        except (BlockedError, ManualInterventionRequired) as e:
                            breaker.record_block()
                            blocked.append(r.reference)
                            m.count(_mk(spec), "blocked")
                            log(logger, logging.WARNING, "blocked", ref=r.reference, error=str(e))
                        except Exception as e:   # one bad PRO never fails the batch
                            m.count(_mk(spec), "failed")
                            log(logger, logging.WARNING, "track failed", ref=r.reference,
                                error=f"{type(e).__name__}: {e}")
                        finally:
                            m.observe(_mk(spec), time.time() - t)

                await asyncio.gather(*[one(r) for r in batch.refs])
                if blocked:
                    await self.queue.raise_(run_id, spec.code, a.bill_to, blocked, "blocked by carrier site", client=spec.client)
                await self._post(run_id, spec, a, pending_payloads)
                self.store.kv_set(f"last:{spec.code}:{a.bill_to}", str(time.time()))
            finally:
                await adapter.close()

    async def _post(self, run_id, spec, a, items: list[tuple[str, dict]]) -> None:
        if not items:
            return
        n = spec.limits.post_batch_size
        for i in range(0, len(items), n):
            chunk = items[i:i + n]
            if self.s.dry_run:
                log(logger, logging.INFO, "dry-run: would post", count=len(chunk))
                continue
            try:
                await retry(lambda: self.backend.post_tracking(a, spec, run_id, [p for _, p in chunk]),
                            attempts=3, base=2, cap=30, retry_on=(Exception,))
                for k, _ in chunk:        # mark sent ONLY after the backend accepted -> safe to re-run
                    self.store.mark_sent(k)
                self.metrics.count(_mk(spec), "posted", len(chunk))
            except Exception as e:
                self.metrics.count(_mk(spec), "post_failed", len(chunk))
                log(logger, logging.ERROR, "post failed; will resend next run", error=type(e).__name__)
