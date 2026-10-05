"""Live probe for ONE client+carrier.
  python scripts/probe.py calix estes 1234567890 [--headed]
Runs the real adapter and prints: discovered selectors, raw JSON, normalized result (+ what was auto-detected),
and the final backend payload. Pin what you verify in mapping.yaml.
  python scripts/probe.py calix estes --listen      # headed browser: YOU search; every JSON call is printed"""
import asyncio, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.browser.factory import make_provider
from app.core import registry
from app.core.adapter import session_key
from app.core.mapper import build_payload
from app.logging_setup import setup_logging
from app.models import CarrierAccount
from app.settings import load_resolver, load_settings


def shape(o, d=0):
    if isinstance(o, dict) and d < 2:
        return {k: shape(v, d + 1) for k, v in list(o.items())[:15]}
    if isinstance(o, list):
        return [shape(o[0], d + 1)] if o else []
    return type(o).__name__


async def main(client, carrier, rest):
    s = load_settings("config/app.yaml"); setup_logging("WARNING"); registry.discover()
    spec = load_resolver(s.clients_dir).resolve(client, carrier)
    assert spec, f"{client}/{carrier} not configured (see config errors above)"
    accounts = await __import__("app.backend.client", fromlist=["x"]).make_backend(s.backend).get_carrier_accounts()
    acct = next((a for a in accounts if a.bill_to.upper() == client.upper() and a.carrier_code.upper() == spec.code), None) \
        or CarrierAccount(id=0, carrierName=spec.code, carrierCode=spec.code, billTo=client.upper())
    headed = "--headed" in rest or "--listen" in rest
    cfg = spec.browser.model_copy(update={"headless": False}) if headed else spec.browser
    prov = make_provider(cfg, s.state_dir)
    if "--listen" in rest:
        sess = await prov.open_session(session_key(spec))
        def on_resp(r):
            if "json" in (r.headers.get("content-type") or ""):
                async def show():
                    try: print(f"\n[{r.status}] {r.url}\n   shape: {json.dumps(shape(await r.json()))[:400]}")
                    except Exception: pass
                asyncio.create_task(show())
        sess.page.on("response", on_resp)
        await sess.goto(spec.site.tracking_url or acct.tracking_url)
        print("Browser open. Search a PRO. JSON calls print here. Press Enter when done.")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await sess.close(); await prov.close(); return
    ref = next(a for a in rest if not a.startswith("--"))
    ad = registry.get_adapter_class(spec.code, spec.client)(spec, acct, prov)
    try:
        await ad.authenticate()
        print("SELECTORS:", getattr(ad, "_sel", "n/a"))
        raw = await ad.track(ref, "PRO")
        print("\nRAW (first 1500 chars):", json.dumps(raw, indent=2)[:1500])
        res = ad.normalize(raw, ref, "PRO")
        print("\nNORMALIZED:", res.model_dump_json(exclude={"raw"}, indent=2))
        print("\nPAYLOAD:", json.dumps(build_payload(spec, res, acct, "probe"), indent=2))
    finally:
        await ad.close(); await prov.close()

asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
