"""Single orchestration path: backend intake -> browser/network capture -> mapping -> backend update."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from app.core.batch_updates import enqueue_tracking_updates
from app.core.diagnostics import log_environment_report, setup_asyncio_exception_handler
from app.core.intake import build_jobs
from app.carriers.adapter_registry import carrier_adapter_registry
from app.logging_setup import carrier_var, client_var, log, register_secret, run_file_logger, run_id_var
from app.settings import CarrierSpec, SpecResolver

logger = logging.getLogger(__name__)


class Runner:
    def __init__(self, sv, resolver: SpecResolver):
        self.sv, self.resolver = sv, resolver
        self._run_lock = asyncio.Lock()
        self._last_run: dict[tuple[str, str, int], float] = {}  # process-local scheduler only
        self.last_results: list[dict] = []
        self.last_log_file = None

    def _due(self, config, spec: CarrierSpec, force: bool) -> bool:
        if force:
            return True
        frequency = float(config.processing_frequency or 0)
        if frequency <= 0:
            return True
        key = (spec.client, spec.code, int(config.bill_to))
        last = self._last_run.get(key)
        # API processingFrequency is interpreted as minutes (e.g. 1439 ~= once daily).
        return last is None or time.time() - last >= frequency * 60

    async def run_once(self, only: set[str] | None = None, force: bool = False,
                       only_clients: set[str] | None = None) -> dict:
        async with self._run_lock:
            run_id = uuid.uuid4().hex[:12]
            run_id_var.set(run_id)
            with run_file_logger(run_id=run_id, logs_dir=self.sv.settings.logs_dir) as log_file:
                self.last_log_file = log_file
                logger.info("run started | run_id=%s log_file=%s", run_id, log_file)
                try:
                    setup_asyncio_exception_handler(asyncio.get_running_loop(), logger)
                    log_environment_report(self.sv.settings, self.resolver, logger)
                except Exception as exc:
                    logger.debug("optional diagnostics setup skipped: %s", type(exc).__name__)
                result = await self._run_once(only, force, only_clients)
                logger.info("run finished | run_id=%s log_file=%s", run_id, log_file)
                return result

    async def _run_once(self, only: set[str] | None, force: bool, only_clients: set[str] | None) -> dict:
        started = time.time()
        self.last_results = []
        summary: dict[str, dict] = {}

        def selected(config):
            spec = self.resolver.resolve(str(config.bill_to), config.carrier_code, config=config)
            if only:
                identifiers = {config.carrier_code.upper()}
                if spec is not None:
                    identifiers.add(spec.code.upper())
                    identifiers.update(alias.upper() for alias in spec.aliases)
                if not identifiers.intersection(only):
                    return False
            customer_name = (config.customer_name or "").strip().upper()
            if only_clients and (spec is None or spec.client.upper() not in only_clients) and str(config.bill_to).upper() not in only_clients and customer_name not in only_clients:
                return False
            if spec is None:
                summary[f"{config.bill_to}/{config.carrier_code}"] = {
                    "requested": 0, "failures": 1, "error": "unsupported carrier profile"}
                logger.error("no built-in carrier profile | bill_to=%s carrier_code=%s",
                             config.bill_to, config.carrier_code)
                return False
            if not self._due(config, spec, force):
                summary[f"{spec.client}/{spec.code}"] = {"requested": 0, "skipped_not_due": 1}
                return False
            return True

        def intake_empty(config):
            spec = self.resolver.resolve(str(config.bill_to), config.carrier_code, config=config)
            if spec is not None:
                self._last_run[(spec.client, spec.code, int(config.bill_to))] = time.time()

        grouped = await build_jobs(self.sv.backend, include_config=selected, on_empty=intake_empty)
        groups: dict[tuple[int, str], list] = {}
        for carriers in grouped.values():
            for carrier_code, jobs in carriers.items():
                for job in jobs:
                    groups.setdefault((job.carrier.bill_to, carrier_code), []).append(job)
        active_groups = 0

        async def scrape_group(key: tuple[int, str], jobs: list) -> None:
            nonlocal active_groups
            bill_to, code = key
            config = jobs[0].carrier
            spec = self.resolver.resolve(str(bill_to), code, config=config)
            if spec is None:
                summary[f"{bill_to}/{code}"] = {"requested": len(jobs), "failures": len(jobs),
                                                "error": "unsupported carrier profile"}
                logger.error("no built-in carrier interaction profile | bill_to=%s carrier_code=%s", bill_to, code)
                return
            active_groups += 1
            for job in jobs:
                if job.carrier.password:
                    register_secret(job.carrier.password.get_secret_value())
            client_var.set(spec.client)
            carrier_var.set(spec.code)
            provider = self.sv.provider_factory(spec.browser, self.sv.settings.browser_state_dir)
            try:
                adapter = carrier_adapter_registry.create(provider, spec, self.sv.settings.debug_dir)
                outcome = await adapter.track_batch(jobs)
                activity_stats = await enqueue_tracking_updates(
                    self.sv.settings, self.sv.backend, config, spec, run_id_var.get() or "", outcome, jobs)
                label_key = f"{spec.client}/{spec.code}"
                found_n = sum(1 for value in outcome.outcomes.values() if value == "found")
                not_found_n = sum(1 for value in outcome.outcomes.values() if value == "not_found")
                summary[label_key] = {
                    "requested": len(outcome.requested), "received": len(outcome.received),
                    "found": found_n, "not_found": not_found_n, "missing": len(outcome.missing),
                    "errors": len(outcome.errors), "activity_posted": activity_stats.get("posted", 0),
                    "activity_skipped": activity_stats.get("skipped", 0),
                    "activity_failed": activity_stats.get("failed", 0),
                }
                self.last_results.append({"client": spec.client, "carrier": spec.code,
                                          "bill_to": bill_to, "result": outcome})
                log(logger, logging.INFO, "carrier batch complete", client=spec.client,
                    carrier_code=spec.code, bill_to=bill_to, requested=len(outcome.requested),
                    received=len(outcome.received), found=found_n, not_found=not_found_n,
                    missing=len(outcome.missing), failures=len(outcome.errors),
                    activity_posted=activity_stats.get("posted", 0),
                    activity_failed=activity_stats.get("failed", 0), first_error=outcome.first_error or "")
                if outcome.error_type == "ChallengeError" or any(
                        err.get("type") == "ChallengeError" for err in outcome.errors):
                    blocked_refs = [j.search_value for j in jobs if outcome.outcomes.get(j.search_value) == "error"]
                    await self.sv.queue.raise_(run_id_var.get() or "", spec.client, spec.code, str(bill_to),
                                               blocked_refs or outcome.requested,
                                               outcome.error_message or "Carrier anti-bot challenge requires intervention",
                                               spec.site.tracking_url or config.tracking_url)
                # Do not postpone retries for a carrier-level failure/challenge. The backend remains
                # authoritative for whether a delivered result is new, changed, or already processed.
                if not outcome.error_type:
                    self._last_run[(spec.client, spec.code, int(bill_to))] = time.time()
            finally:
                try:
                    await provider.close()
                except Exception as exc:
                    logger.warning("browser provider close failed | carrier=%s error=%s", code, type(exc).__name__)

        # Run carrier groups sequentially: one browser lifecycle at a time avoids parallel
        # browser/profile contention and keeps CAPTCHA/session behavior predictable.
        for (bill_to, code), jobs in groups.items():
            try:
                await scrape_group((bill_to, code), jobs)
            except Exception as exc:
                summary[f"{bill_to}/{code}"] = {"failures": 1, "error": type(exc).__name__}
                logger.exception("carrier batch failed | bill_to=%s carrier_code=%s", bill_to, code)
        log(logger, logging.INFO, "run complete", run_id=run_id_var.get(),
            seconds=round(time.time() - started, 1), carriers_fetched=len(groups),
            carriers_processed=active_groups, jobs_built=sum(len(j) for j in groups.values()), summary=summary)
        return {**summary, "scrape_results": self.last_results}
