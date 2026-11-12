"""Live probe for one client, carrier, and PRO.

    python scripts/probe.py calix estes 1234567890 [--headed]
    python scripts/probe.py calix estes [--listen]

The probe uses the same browser capture, normalization, and payload builder as the app.
"""
import argparse
import asyncio
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.backend.client import make_backend
from app.browser.factory import make_provider
from app.browser.session_utils import session_key
from app.carriers.adapter_registry import carrier_adapter_registry
from app.core.carrier_activity import (
    account_from_config,
    backend_ml_status,
    build_add_carrier_activity,
)
from app.core.mapper import normalize
from app.logging_setup import setup_logging
from app.models import ShipmentRef, TrackingJob
from app.settings import load_resolver, load_settings


def shape(value, depth=0):
    if isinstance(value, dict) and depth < 2:
        return {key: shape(item, depth + 1) for key, item in list(value.items())[:15]}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def find_carrier_config(configs, resolver, client, carrier):
    client_key = client.strip().casefold()
    carrier_key = carrier.strip().casefold()
    for config in configs:
        spec = resolver.resolve(str(config.bill_to), config.carrier_code, config=config)
        if spec is None:
            continue
        client_names = {str(config.bill_to).casefold()}
        if config.customer_name:
            client_names.add(config.customer_name.strip().casefold())
        carrier_names = {spec.code.casefold(), config.carrier_code.strip().casefold()}
        carrier_names.update(alias.casefold() for alias in spec.aliases)
        if client_key in client_names and carrier_key in carrier_names:
            return config, spec
    return None, None


async def listen_for_responses(provider, spec, config):
    session = await provider.open_session(session_key(spec))
    try:
        await session.goto(spec.site.tracking_url or config.tracking_url)

        async def wait_for_search():
            print("Browser open. Search the PRO, then press Enter here.")
            await asyncio.get_running_loop().run_in_executor(None, input)

        responses = await session.capture_all(wait_for_search, timeout_s=5, settle_s=2)
        for response in responses:
            print(f"\n[{response.status}] {response.url}")
            print("SHAPE:", json.dumps(shape(response.json)))
            print("JSON:", json.dumps(response.json, indent=2, default=str))
        await session.save_state()
    finally:
        await session.close()


async def probe(client, carrier, pro, headed, listen):
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_format, settings.logs_dir)
    resolver = load_resolver()
    backend = make_backend(settings.backend)
    provider = None
    try:
        configs = await backend.get_carrier_configs()
        config, spec = find_carrier_config(configs, resolver, client, carrier)
        if config is None or spec is None:
            raise SystemExit(
                f"No backend carrier configuration matched client={client!r}, carrier={carrier!r}"
            )
        if not (spec.site.tracking_url or config.tracking_url):
            raise SystemExit(f"No tracking URL is configured for {client}/{carrier}")

        if headed or listen:
            spec = spec.model_copy(update={
                "browser": spec.browser.model_copy(update={"headless": False})
            })
        provider = make_provider(spec.browser, settings.browser_state_dir)

        if listen:
            await listen_for_responses(provider, spec, config)
            return
        if not pro:
            raise SystemExit("A PRO number is required unless --listen is used")

        print("SELECTORS:", {
            "input": spec.site.input_selector,
            "submit": spec.site.submit_selector,
            "response_url_pattern": spec.site.response_url_pattern,
        })
        shipment = ShipmentRef(
            housebill="",
            proNumber=pro,
            vendorId=config.carrier_code,
        )
        job = TrackingJob(
            carrier=config,
            shipment=shipment,
            search_by="proNumber",
            search_value=pro,
        )

        started = datetime.now(timezone.utc)
        adapter = carrier_adapter_registry.create(provider, spec, settings.debug_dir)
        scraped = await adapter.track_batch([job])
        ended = datetime.now(timezone.utc)
        raw = scraped.received.get(pro)
        if raw is None:
            print("SCRAPE:", json.dumps({
                "outcome": scraped.outcomes.get(pro),
                "summary": scraped.summaries.get(pro, {}),
                "errors": scraped.errors,
            }, indent=2, default=str))
            raise SystemExit(f"No carrier JSON captured for PRO {pro}")

        print("\nRAW:", json.dumps(raw, indent=2, default=str))
        account = account_from_config(config)
        result = normalize(spec, raw, pro, "PRO", account)
        print("\nNORMALIZED:", result.model_dump_json(exclude={"raw"}, indent=2))
        payload = build_add_carrier_activity(
            job,
            result,
            outcome="found",
            summary=scraped.summaries.get(pro, {}),
            ml_start=started,
            ml_end=ended,
            ml_status=backend_ml_status(spec, "ok"),
            ml_log="probe",
            spec=spec,
        )
        print("\nPAYLOAD:", json.dumps(payload, indent=2, default=str))
    finally:
        if provider is not None:
            await provider.close()
        await backend.close()


def main():
    parser = argparse.ArgumentParser(description="Probe one carrier tracking response.")
    parser.add_argument("client")
    parser.add_argument("carrier")
    parser.add_argument("pro", nargs="?")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--listen", action="store_true")
    args = parser.parse_args()
    asyncio.run(probe(args.client, args.carrier, args.pro, args.headed, args.listen))


if __name__ == "__main__":
    main()
