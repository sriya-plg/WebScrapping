"""Composition root + CLI.
  python -m app.main run-once [--client calix] [--carrier estes] [--force] [--dry-run]
  python -m app.main daemon
  python -m app.main assist saia --client calix     # headed browser: solve the challenge once; cookies are saved"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from app.backend.client import make_backend
from app.backend.apis import CarrierConfigurationBackend
from app.browser.factory import make_provider
from app.core import registry
from app.core.adapter import session_key
from app.core.intervention import InterventionQueue
from app.core.job import Services
from app.core.runner import Runner
from app.core.scheduler import JitterSchedule
from app.core.state import StateStore
from app.logging_setup import setup_logging
from app.settings import load_resolver, load_settings

logger = logging.getLogger("main")


def build(args):
    s = load_settings(args.config)
    if os.environ.get("CARRIER_CONFIGURATION_API_BASE_URL"):
        s.carrier_configuration.base_url = os.environ["CARRIER_CONFIGURATION_API_BASE_URL"]
    s.dry_run = s.dry_run or getattr(args, "dry_run", False)
    setup_logging(s.log_level)
    for e in registry.discover():
        logger.error("adapter import failed: %s", e)
    store = StateStore(s.state_db)
    resolver = load_resolver(s.clients_dir)
    backend = make_backend(s.backend)
    if s.carrier_configuration.enabled:
        backend = CarrierConfigurationBackend(s.carrier_configuration, backend)
    sv = Services(s, backend, store, InterventionQueue(store, s.alerts), make_provider)
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
    await asyncio.get_event_loop().run_in_executor(None, input, "Solve the challenge in the browser, then press Enter... ")
    await sess.close()
    await provider.close()
    sv.store.resolve_interventions(spec.client, spec.code)
    print("session saved; the next run reuses it and the intervention queue was cleared.")


async def daemon(s, runner, store):
    cfg = s.schedule
    anchor = float(store.kv_get("schedule_anchor") or 0) or time.time()
    store.kv_set("schedule_anchor", str(anchor))
    sched = JitterSchedule(anchor, cfg.interval_minutes * 60, cfg.jitter_minutes * 60, cfg.seed)
    try:
        while True:
            at = sched.next_after(time.time())
            logger.info("next run at %s", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)))
            await asyncio.sleep(max(0, at - time.time()))
            try:
                await runner.run_once()
            except Exception:
                logger.exception("run crashed; daemon continues")
    finally:
        close = getattr(runner.sv.backend, "close", None)
        if close:
            await close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/app.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run-once")
    r.add_argument("--client", action="append", help="client key or billTo, e.g. calix")
    r.add_argument("--carrier", action="append")
    r.add_argument("--force", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("daemon")
    a = sub.add_parser("assist")
    a.add_argument("carrier")
    a.add_argument("--client", required=True)
    args = ap.parse_args()
    s, sv, resolver, runner = build(args)
    if args.cmd == "run-once":
        async def run_once():
            try:
                await runner.run_once({c.upper() for c in args.carrier} if args.carrier else None, args.force,
                                      {c.upper() for c in args.client} if args.client else None)
            finally:
                close = getattr(sv.backend, "close", None)
                if close:
                    await close()
        asyncio.run(run_once())
    elif args.cmd == "daemon":
        asyncio.run(daemon(s, runner, sv.store))
    else:
        asyncio.run(assist(s, sv, resolver, args.carrier, args.client))


if __name__ == "__main__":
    main()
