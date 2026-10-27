"""Composition root + CLI.
  python -m app.main run-once [--client calix] [--carrier estes] [--force] [--dry-run]
  python -m app.main daemon
  python -m app.main assist saia --client calix     # headed browser: solve the challenge once; cookies are saved"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import signal
import time
from datetime import datetime, timedelta

from app.backend.client import make_backend
from app.browser.factory import make_provider
from app.core import registry
from app.core.adapter import session_key
from app.core.intervention import InterventionQueue
from app.core.job import Services
from app.core.runner import Runner
from app.core.state import StateStore
from app.logging_setup import setup_logging
from app.settings import load_resolver, load_settings

logger = logging.getLogger("main")


def build(args):
    s = load_settings(args.config)
    s.dry_run = s.dry_run or getattr(args, "dry_run", False)
    setup_logging(s.log_level, s.log_format, s.logs_dir)
    for e in registry.discover():
        logger.error("adapter import failed: %s", e)
    store = StateStore(s.state_db)
    resolver = load_resolver(s.clients_dir)
    sv = Services(s, make_backend(s.backend), store, InterventionQueue(store, s.alerts), make_provider)
    return s, sv, resolver, Runner(sv, resolver)


async def assist(s, sv, resolver, code: str, client: str):
    spec = resolver.resolve(client, code)
    if spec is None:
        raise SystemExit(f"{client}/{code} is not configured")
    url = spec.site.tracking_url
    if not url:
        accounts = await sv.backend.get_carrier_accounts()
        url = next((a.tracking_url for a in accounts if a.bill_to.upper() == client.upper()
                    and a.carrier_code.upper() in (spec.code, *[x.upper() for x in spec.aliases])), None)
    if not url:
        raise SystemExit("no tracking URL found (backend trackingUrl / site.tracking_url)")
    provider = make_provider(spec.browser.model_copy(update={"headless": False}), s.state_dir)
    sess = await provider.open_session(session_key(spec))      # SAME key the adapter uses
    await sess.goto(url)
    await asyncio.get_running_loop().run_in_executor(None, input, "Solve the challenge in the browser, then press Enter... ")
    await sess.close()
    await provider.close()
    sv.store.resolve_interventions(spec.client, spec.code)
    print("session saved; the next run reuses it and the intervention queue was cleared.")


def _read_interval_seconds() -> tuple[float, float]:
    """RUN_INTERVAL_HOURS (default 3) -> (hours, seconds). Must be a positive finite number."""
    hours = float(os.environ.get("RUN_INTERVAL_HOURS", "3"))
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("RUN_INTERVAL_HOURS must be a positive finite number")
    return hours, hours * 3600


def _read_run_timeout_seconds() -> float | None:
    """Optional RUN_TIMEOUT_HOURS. Unset, empty or 0 means no per-run timeout."""
    raw = (os.environ.get("RUN_TIMEOUT_HOURS") or "").strip()
    if not raw:
        return None
    hours = float(raw)
    if not math.isfinite(hours) or hours < 0:
        raise ValueError("RUN_TIMEOUT_HOURS must be a non-negative finite number")
    return hours * 3600 if hours > 0 else None


async def daemon(s, runner, store):
    interval_hours, interval_seconds = _read_interval_seconds()
    run_timeout_s = _read_run_timeout_seconds()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handlers = {}
    state = {"stop_requests": 0, "run_task": None}

    def on_stop_request():
        """Runs on the event loop (not inside the signal handler), so logging is safe here."""
        state["stop_requests"] += 1
        stop_event.set()
        task = state["run_task"]
        running = task is not None and not task.done()
        if state["stop_requests"] == 1:
            if running:
                logger.info("shutdown requested; finishing the current run "
                            "(press Ctrl+C again to cancel it immediately)")
            else:
                logger.info("shutdown requested")
        elif running:
            logger.warning("second shutdown request; cancelling the current run")
            task.cancel()

    def request_stop(_signum, _frame):
        loop.call_soon_threadsafe(on_stop_request)

    signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals.append(signal.SIGTERM)

    try:
        for signum in signals:
            try:
                previous_handlers[signum] = signal.signal(signum, request_stop)
            except (OSError, ValueError) as exc:
                logger.warning("could not register shutdown signal %s: %s", signum, exc)

        logger.info("daemon started, running first job now (interval_hours=%s, run_timeout=%s)",
                    interval_hours, f"{run_timeout_s / 3600:g}h" if run_timeout_s else "none")
        while not stop_event.is_set():
            run_started = time.monotonic()
            run_task = asyncio.create_task(runner.run_once())
            state["run_task"] = run_task
            try:
                done, _ = await asyncio.wait({run_task}, timeout=run_timeout_s)
                if done:
                    run_task.result()          # re-raises a crash from the run, if any
                else:
                    logger.error("run exceeded RUN_TIMEOUT_HOURS (%.1f hours); cancelling it, daemon continues",
                                 run_timeout_s / 3600)
                    run_task.cancel()
                    await asyncio.gather(run_task, return_exceptions=True)
            except asyncio.CancelledError:
                if state["stop_requests"] >= 2:
                    logger.warning("run cancelled by second shutdown request")
                    break
                raise
            except Exception:
                logger.exception("run crashed; daemon continues")
            finally:
                if not run_task.done():
                    run_task.cancel()
                state["run_task"] = None

            run_finished = time.monotonic()
            duration = run_finished - run_started
            if stop_event.is_set():
                logger.info("run finished in %.1f seconds; shutdown requested, no next run scheduled", duration)
                break

            remaining = max(0.0, run_started + interval_seconds - run_finished)
            next_run_at = datetime.now().astimezone() + timedelta(seconds=remaining)
            logger.info("run finished in %.1f seconds, next run at %s",
                        duration, next_run_at.strftime("%Y-%m-%d %H:%M:%S %Z"))
            logger.info("daemon sleeping for %.1f seconds", remaining)

            if remaining == 0:
                await asyncio.sleep(0)
                continue

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=remaining)
                logger.info("daemon shutdown requested while waiting")
            except asyncio.TimeoutError:
                continue
            break
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        logger.info("daemon stopped")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/app.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run-once")
    r.add_argument("--client", action="append", help="client key or billTo, e.g. calix")
    r.add_argument("--carrier", action="append")
    r.add_argument("--force", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--headless", action="store_true", default=None, help="Force headless browser mode")
    r.add_argument("--headed", action="store_true", default=None, help="Force headed browser mode")
    d = sub.add_parser("daemon")
    d.add_argument("--headless", action="store_true", default=None, help="Force headless browser mode")
    d.add_argument("--headed", action="store_true", default=None, help="Force headed browser mode")
    a = sub.add_parser("assist")
    a.add_argument("carrier")
    a.add_argument("--client", required=True)
    args = ap.parse_args()
    if getattr(args, "headless", None):
        os.environ["BROWSER_HEADLESS"] = "true"
    elif getattr(args, "headed", None):
        os.environ["BROWSER_HEADLESS"] = "false"
    s, sv, resolver, runner = build(args)
    if args.cmd == "run-once":
        asyncio.run(runner.run_once({c.upper() for c in args.carrier} if args.carrier else None, args.force,
                                    {c.upper() for c in args.client} if args.client else None))
    elif args.cmd == "daemon":
        asyncio.run(daemon(s, runner, sv.store))
    else:
        asyncio.run(assist(s, sv, resolver, args.carrier, args.client))


if __name__ == "__main__":
    main()