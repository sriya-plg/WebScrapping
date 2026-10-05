"""One (billTo, carrier) job. Stages are separate so a failure in one is contained:
  pending -> authenticate -> [per ref: track | map] -> post
 - a bad ref never fails the batch      - a mapping/hook error fails only that ref (raw sample is saved)
 - a block aborts the carrier cleanly   - a post failure keeps items for the next run (idempotent)"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.backend.client import BackendClient
from app.core.intervention import InterventionQueue
from app.core.mapper import build_payload
from app.core.metrics import RunMetrics
from app.core.registry import get_adapter_class
from app.core.resilience import (BlockedError, CircuitBreaker, ManualInterventionRequired, NotFoundError,
                                 Pacer, retry)
from app.core.state import KVCache, StateStore
from app.logging_setup import log
from app.models import CarrierAccount, PendingRef, TrackingResult
from app.settings import AppSettings, CarrierSpec

logger = logging.getLogger(__name__)


@dataclass
class Services:                      # long-lived collaborators, injected once (composition root = main.py)
    settings: AppSettings
    backend: BackendClient
    store: StateStore
    queue: InterventionQueue
    provider_factory: Callable


@dataclass
class CarrierGuard:                  # shared per carrier SITE across clients (the block is on our IP)
    pacer: Pacer
    breaker: CircuitBreaker
    lock: asyncio.Lock


@dataclass
class RunContext:                    # per run
    run_id: str
    metrics: RunMetrics
    providers: dict = field(default_factory=dict)
    sampled: set = field(default_factory=set)


def label(spec: CarrierSpec) -> str:
    return f"{spec.client}/{spec.code}"


def idempotency_key(spec: CarrierSpec, bill_to: str, res: TrackingResult) -> str:
    body = res.model_dump(mode="json", exclude={"raw", "scraped_at", "notes"})
    return hashlib.sha1(f"{spec.client}|{spec.code}|{bill_to}|{json.dumps(body, sort_keys=True)}".encode()).hexdigest()


class AccountJob:
    def __init__(self, sv: Services, ctx: RunContext, spec: CarrierSpec, account: CarrierAccount, guard: CarrierGuard):
        self.sv, self.ctx, self.spec, self.a, self.g = sv, ctx, spec, account, guard
        self.m, self.lbl = ctx.metrics, label(spec)

    async def run(self) -> None:
        async with self.g.lock:                       # accounts of one carrier run one after another
            if not self.g.breaker.allow():
                self.m.count(self.lbl, "skipped_circuit_open")
                log(logger, logging.WARNING, "circuit open; skipping", bill_to=self.a.bill_to)
                return
            batch = await retry(lambda: self.sv.backend.get_pending(self.a, self.spec), attempts=3, base=2,
                                cap=30, retry_on=(Exception,))
            if not batch.refs:
                self._touch()
                return
            self.m.count(self.lbl, "pending", len(batch.refs))
            adapter = self._make_adapter()
            try:
                if not await self._authenticate(adapter, batch.refs):
                    return
                items, blocked, ok = await self._track_all(adapter, batch.refs)
                if blocked:
                    await self.sv.queue.raise_(self.ctx.run_id, self.spec.client, self.spec.code, self.a.bill_to,
                                               blocked, "blocked by carrier site")
                await self._post(items)
                if not blocked:
                    self._touch()
                    if ok:
                        self.sv.store.resolve_interventions(self.spec.client, self.spec.code)
            finally:
                try:
                    await adapter.close()
                except Exception as e:
                    log(logger, logging.WARNING, "adapter close failed", error=type(e).__name__)

    # ---- stages ---------------------------------------------------------------------------------
    def _make_adapter(self):
        key = f"{self.spec.client}:{self.spec.code}"
        if key not in self.ctx.providers:
            self.ctx.providers[key] = self.sv.provider_factory(self.spec.browser, self.sv.settings.state_dir)
        cls = get_adapter_class(self.spec.code, self.spec.client)
        return cls(self.spec, self.a, self.ctx.providers[key], KVCache(self.sv.store, self.spec.client, self.spec.code))

    async def _authenticate(self, adapter, refs: list[PendingRef]) -> bool:
        try:
            await retry(adapter.authenticate, attempts=2, base=3, cap=20)
            return True
        except (BlockedError, ManualInterventionRequired) as e:
            self.g.breaker.record_block()
            self.m.count(self.lbl, "blocked", len(refs))
            await self.sv.queue.raise_(self.ctx.run_id, self.spec.client, self.spec.code, self.a.bill_to,
                                       [r.reference for r in refs], str(e), getattr(e, "url", None))
        except Exception as e:                        # e.g. browser failed to start: this carrier only
            self.m.count(self.lbl, "auth_failed", len(refs))
            log(logger, logging.ERROR, "authenticate failed", error=f"{type(e).__name__}: {e}")
        return False

    async def _track_all(self, adapter, refs: list[PendingRef]):
        items: list[tuple[str, dict]] = []
        blocked: list[str] = []
        ok = [0]
        L = self.spec.limits

        async def one(r: PendingRef):
            if self.g.breaker.is_open:                # tripped mid-batch: stop hitting the site
                blocked.append(r.reference)
                return
            raw = None
            async with self.g.pacer:
                t = time.time()
                try:
                    raw = await retry(lambda: adapter.track(r.reference, r.ref_type), attempts=L.retries,
                                      base=L.backoff_base_s, cap=L.backoff_cap_s)
                    self.g.breaker.record_success()
                except NotFoundError:
                    self.m.count(self.lbl, "not_found")
                except (BlockedError, ManualInterventionRequired) as e:
                    self.g.breaker.record_block()
                    blocked.append(r.reference)
                    self.m.count(self.lbl, "blocked")
                    log(logger, logging.WARNING, "blocked", ref=r.reference, error=str(e))
                except Exception as e:                # one bad PRO never fails the batch
                    self.m.count(self.lbl, "failed")
                    log(logger, logging.WARNING, "track failed", ref=r.reference, error=f"{type(e).__name__}: {e}")
                finally:
                    self.m.observe(self.lbl, time.time() - t)
            if raw is None:
                return
            try:                                      # mapping stage: isolated from tracking
                res = adapter.normalize(raw, r.reference, r.ref_type)
                payload = build_payload(self.spec, res, self.a, self.ctx.run_id, r.meta)
                key = idempotency_key(self.spec, self.a.bill_to, res)
            except Exception as e:
                self.m.count(self.lbl, "map_failed")
                self._sample(r.reference, raw)
                log(logger, logging.ERROR, "mapping failed", ref=r.reference, error=f"{type(e).__name__}: {e}")
                return
            ok[0] += 1
            if res.status == "UNKNOWN":
                self.m.count(self.lbl, "unknown_status")
                self._sample(r.reference, raw)
            if res.notes:
                log(logger, logging.DEBUG, "auto-detected", ref=r.reference, notes=res.notes)
            if self.sv.store.was_sent(key):
                self.m.count(self.lbl, "unchanged")
            else:
                items.append((key, payload))
                self.m.count(self.lbl, "success")

        await asyncio.gather(*[one(r) for r in refs])
        return items, blocked, ok[0]

    async def _post(self, items: list[tuple[str, dict]]) -> None:
        n = self.spec.limits.post_batch_size
        for i in range(0, len(items), n):
            chunk = items[i:i + n]
            if self.sv.settings.dry_run:
                log(logger, logging.INFO, "dry-run: would post", count=len(chunk))
                continue
            try:
                await retry(lambda: self.sv.backend.post_tracking(self.a, self.spec, self.ctx.run_id,
                                                                  [p for _, p in chunk]),
                            attempts=3, base=2, cap=30, retry_on=(Exception,))
                for k, _ in chunk:                    # mark sent ONLY after the backend accepted
                    self.sv.store.mark_sent(k)
                self.m.count(self.lbl, "posted", len(chunk))
            except Exception as e:
                self.m.count(self.lbl, "post_failed", len(chunk))
                log(logger, logging.ERROR, "post failed; will resend next run", error=type(e).__name__)

    # ---- helpers --------------------------------------------------------------------------------
    def _touch(self) -> None:
        self.sv.store.kv_set(f"last:{self.spec.client}:{self.spec.code}:{self.a.bill_to}", str(time.time()))

    def _sample(self, ref: str, raw: dict) -> None:
        """Save one raw response per carrier per run: the input you need to write/fix mapping.yaml."""
        if self.lbl in self.ctx.sampled:
            return
        self.ctx.sampled.add(self.lbl)
        try:
            d = Path(self.sv.settings.samples_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{self.spec.client}_{self.spec.code}.json").write_text(json.dumps(raw, indent=2, default=str))
        except Exception:
            pass
