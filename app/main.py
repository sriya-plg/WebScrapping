"""Application entry point. Carrier runtime configuration is fetched from the backend API."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timedelta

from app.backend.client import make_backend
from app.browser.factory import make_provider
from app.browser.session_utils import session_key
from app.core.intervention import InterventionQueue
from app.core.runner import Runner
from app.core.services import Services
from app.logging_setup import append_run_output, setup_logging
from app.settings import load_resolver, load_settings

logger = logging.getLogger("main")


def build(args=None):
    settings = load_settings()
    settings.dry_run = settings.dry_run or bool(getattr(args, "dry_run", False))
    setup_logging(settings.log_level, settings.log_format, settings.logs_dir)
    resolver = load_resolver()
    for error in resolver.errors:
        logger.error("built-in carrier profile failed validation: %s", error)
    backend = make_backend(settings.backend)
    services = Services(settings, backend, InterventionQueue(settings.alerts), make_provider)
    return settings, services, resolver, Runner(services, resolver)


async def assist(settings, services, resolver, code: str, client: str | None = None):
    """Open the backend-provided tracking URL in a headed browser for manual challenge handling."""
    configs = await services.backend.get_carrier_configs()
    target = code.strip().upper()
    def matches(config):
        candidate = resolver.resolve(str(config.bill_to), config.carrier_code, config)
        return bool(candidate and target in {candidate.code.upper(), config.carrier_code.upper(),
                                              *(alias.upper() for alias in candidate.aliases)})
    config = next((c for c in configs if matches(c)), None)
    if config is None:
        raise SystemExit(f"carrier configuration {code!r} was not returned by the backend")
    spec = resolver.resolve(str(config.bill_to), config.carrier_code, config=config)
    if spec is None:
        raise SystemExit(f"no built-in browser profile for carrier {config.carrier_code!r}")
    if client and client.strip().casefold() not in {spec.client.casefold(), str(config.bill_to).casefold(),
                                                    (config.customer_name or "").casefold()}:
        raise SystemExit("the selected carrier does not match the requested client/billTo")
    if not config.tracking_url:
        raise SystemExit("backend carrier configuration has no trackingUrl")
    provider = make_provider(spec.browser.model_copy(update={"headless": False}), settings.browser_state_dir)
    session = None
    try:
        session = await provider.open_session(session_key(spec))
        await session.goto(config.tracking_url)
        await asyncio.get_running_loop().run_in_executor(None, input,
            "Complete any carrier challenge in the browser, then press Enter to close... ")
        await session.save_state()
    finally:
        if session is not None:
            await session.close()
        await provider.close()


async def daemon(settings, runner):
    interval_hours = float(os.environ.get("RUN_INTERVAL_HOURS", settings.schedule.interval_hours))
    if interval_hours <= 0:
        raise ValueError("RUN_INTERVAL_HOURS must be positive")
    interval_seconds = interval_hours * 3600
    raw_timeout = os.environ.get("RUN_TIMEOUT_HOURS", "").strip()
    timeout_s = float(raw_timeout) * 3600 if raw_timeout else None
    if timeout_s is not None and timeout_s <= 0:
        timeout_s = None

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    stopping = {"count": 0, "task": None}

    def request_stop():
        stopping["count"] += 1
        stop_event.set()
        task = stopping["task"]
        if stopping["count"] > 1 and task and not task.done():
            task.cancel()

    previous = {}
    def handler(_signum, _frame):
        loop.call_soon_threadsafe(request_stop)

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                previous[sig] = signal.signal(sig, handler)
            except (OSError, ValueError):
                pass
    logger.info("daemon started | interval_hours=%s", interval_hours)
    try:
        while not stop_event.is_set():
            started = time.monotonic()
            task = asyncio.create_task(runner.run_once())
            stopping["task"] = task
            try:
                if timeout_s:
                    await asyncio.wait_for(task, timeout=timeout_s)
                else:
                    await task
            except asyncio.TimeoutError:
                logger.error("run exceeded timeout; cancelling it")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                if stopping["count"] > 1:
                    break
                raise
            except Exception:
                logger.exception("run failed; daemon will continue")
            finally:
                stopping["task"] = None
            remaining = max(0, started + interval_seconds - time.monotonic())
            if stop_event.is_set():
                break
            next_at = datetime.now().astimezone() + timedelta(seconds=remaining)
            logger.info("next run at %s; sleeping %.1f seconds", next_at.strftime("%Y-%m-%d %H:%M:%S %Z"), remaining)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
    finally:
        for sig, old_handler in previous.items():
            signal.signal(sig, old_handler)
        logger.info("daemon stopped")


async def _dispatch(args, settings, services, resolver, runner):
    try:
        if args.cmd == "run-once":
            result = await runner.run_once({v.upper() for v in args.carrier} if args.carrier else None,
                                           args.force,
                                           {v.upper() for v in args.client} if args.client else None)
            output = str(result)
            print(output)
            append_run_output(runner.last_log_file, output)
        elif args.cmd == "daemon":
            await daemon(settings, runner)
        else:
            await assist(settings, services, resolver, args.carrier, args.client)
    finally:
        close = getattr(services.backend, "close", None)
        if close:
            await close()


def main():
    parser = argparse.ArgumentParser(description="Backend-driven freight tracking scraper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    once = sub.add_parser("run-once", help="fetch backend shipments, scrape, and post results")
    once.add_argument("--client", action="append", help="client name or billTo")
    once.add_argument("--carrier", action="append", help="carrier code or built-in alias")
    once.add_argument("--force", action="store_true", help="ignore process-local frequency schedule")
    once.add_argument("--dry-run", action="store_true", help="do not post tracking updates")
    once.add_argument("--headless", action="store_true", default=None)
    once.add_argument("--headed", action="store_true", default=None)
    d = sub.add_parser("daemon", help="run recurring backend-driven scrape cycles")
    d.add_argument("--headless", action="store_true", default=None)
    d.add_argument("--headed", action="store_true", default=None)
    a = sub.add_parser("assist", help="open a carrier website for manual challenge handling")
    a.add_argument("carrier")
    a.add_argument("--client", help="optional client name or billTo")
    args = parser.parse_args()
    if getattr(args, "headless", None):
        os.environ["BROWSER_HEADLESS"] = "true"
    elif getattr(args, "headed", None):
        os.environ["BROWSER_HEADLESS"] = "false"
    settings, services, resolver, runner = build(args)
    asyncio.run(_dispatch(args, settings, services, resolver, runner))


if __name__ == "__main__":
    main()
