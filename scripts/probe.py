"""Live probe for ONE client+carrier using Scrapling page scraping.
  python scripts/probe.py calix saia 77133675090 [--headed] [--dump-html]
Runs the carrier adapter and prints: discovered selectors, Scrapling extracted data,
normalized canonical TrackingResult, and the final backend payload.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.browser.factory import make_provider
from app.core import registry
from app.core.mapper import build_payload
from app.logging_setup import setup_logging
from app.models import CarrierAccount
from app.settings import load_resolver, load_settings


async def main(client: str, carrier: str, rest: list[str]) -> None:
    s = load_settings("config/app.yaml")
    setup_logging("WARNING")
    registry.discover()
    spec = load_resolver(s.clients_dir).resolve(client, carrier)
    assert spec, f"{client}/{carrier} not configured (see config errors above)"

    accounts = await __import__("app.backend.client", fromlist=["x"]).make_backend(s.backend).get_carrier_accounts()
    acct = next(
        (a for a in accounts if a.bill_to.upper() == client.upper() and a.carrier_code.upper() == spec.code),
        None,
    ) or CarrierAccount(id=0, carrierName=spec.code, carrierCode=spec.code, billTo=client.upper())

    headed = "--headed" in rest
    dump_html = "--dump-html" in rest
    cfg = spec.browser.model_copy(update={"headless": False}) if headed else spec.browser
    prov = make_provider(cfg, s.state_dir)

    ref = next(a for a in rest if not a.startswith("--"))
    ad = registry.get_adapter_class(spec.code, spec.client)(spec, acct, prov)
    try:
        await ad.authenticate()
        print("SELECTORS:", getattr(ad, "_sel", "n/a"))
        raw = await ad.track(ref, "PRO")
        print("\nSCRAPLING EXTRACTED DATA:\n", json.dumps(raw, indent=2))
        res = ad.normalize(raw, ref, "PRO")
        print("\nNORMALIZED CANONICAL RESULT:\n", res.model_dump_json(exclude={"raw"}, indent=2))
        print("\nPAYLOAD:\n", json.dumps(build_payload(spec, res, acct, "probe"), indent=2))

        if dump_html and ad.session:
            html = await ad.session.content()
            out_file = pathlib.Path(f"scratch/{client}_{carrier}_{ref}.html")
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(html, encoding="utf-8")
            print(f"\nDumped page HTML to {out_file} ({len(html)} bytes)")
    finally:
        await ad.close()
        await prov.close()


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python scripts/probe.py <client> <carrier> <reference> [--headed] [--dump-html]")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
