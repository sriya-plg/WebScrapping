"""CLI:  python -m app.main run-once [--client calix] [--carrier ESTES] [--force] [--dry-run]
        python -m app.main daemon
        python -m app.main assist saia --client calix   # headed browser: solve the challenge once, cookies are saved"""
from __future__ import annotations

import argparse
import asyncio
import time

from app.backend.client import make_backend
from app.browser.factory import make_provider
from app.core import registry
from app.core.intervention import InterventionQueue
from app.core.runner import Runner
from app.core.scheduler import JitterSchedule
from app.core.state import StateStore
from app.logging_setup import setup_logging
from app.settings import load_resolver, load_settings


def build(args):
    s = load_settings(args.config)
    s.dry_run = s.dry_run or getattr(args, "dry_run", False)
    setup_logging(s.log_level)
    registry.discover()
    store = StateStore(s.state_db)
    specs = load_resolver(s.clients_dir)
    return s, store, specs, Runner(s, make_backend(s.backend), store, InterventionQueue(store, s.alerts), specs)


async def assist(s, store, specs, code: str, client: str):
    spec = specs.resolve(client, code)
    cfg = spec.browser.model_copy(update={"headless": False})
    provider = make_provider(cfg, s.state_dir)
    sess = await provider.open_session(spec.code)
    await sess.goto(spec.adapter["tracking_page"])
    await asyncio.get_event_loop().run_in_executor(None, input, "Solve the challenge in the browser, then press Enter... ")
    await sess.close()
    await provider.close()
    store.resolve_interventions(spec.code)
    print("session saved; the next run reuses it and the intervention queue was cleared.")


async def daemon(s, runner, store):
    sched_cfg = s.schedule
    anchor = float(store.kv_get("schedule_anchor") or 0) or time.time()
    store.kv_set("schedule_anchor", str(anchor))
    sched = JitterSchedule(anchor, sched_cfg.interval_minutes * 60, sched_cfg.jitter_minutes * 60, sched_cfg.seed)
    while True:
        at = sched.next_after(time.time())
        await asyncio.sleep(max(0, at - time.time()))
        await runner.run_once()


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
    s, store, specs, runner = build(args)
    if args.cmd == "run-once":
        asyncio.run(runner.run_once({c.upper() for c in args.carrier} if args.carrier else None, args.force,
                                    {c.upper() for c in args.client} if args.client else None))
    elif args.cmd == "daemon":
        asyncio.run(daemon(s, runner, store))
    else:
        asyncio.run(assist(s, store, specs, args.carrier, args.client))


if __name__ == "__main__":
    main()
