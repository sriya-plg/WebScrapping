# Freight tracker -- everything for a client lives in ONE folder

```
app/clients/calix/
  client.yaml                      billTo codes + settings shared by all Calix carriers
                                   (pending-housebills API shape, backend payload keys, default limits)
  carriers/
    estes/      config.yaml        selectors, response regex, field paths, status map, browser, limits
                adapter.py         how to track on this site (authenticate / track)
                hooks.py           optional payload tweaks (auto-used if present)
    saia/       (same 3 files)
    forwardair/ (same 3 files)
app/core/      shared engine (runner, scheduler, retries, breaker, adapter base, state, alerts)
app/browser/   BrowserProvider (playwright_stealth | patchright | crawl4ai stub)
app/backend/   backend API client (+ local-JSON stub)
```
Resolution: backend record (billTo, carrierCode) -> client folder (by billTo in client.yaml)
-> carriers/<carrierCode>/ ; settings = client.yaml `defaults` overridden by the carrier's config.yaml.
Folder name = carrier code (aliases in config.yaml). Adapters are auto-discovered by folder; no registration.
A carrier with no folder under the client is not processed (logged as not_configured).
Rate limits / circuit breaker are shared per carrier site across clients; cookie profiles, metrics and alerts are per client+carrier.

New client:   python scripts/new_client.py acme ACME01 "Acme"
New carrier:  python scripts/new_carrier.py calix forwardair "Forward Air"   (then fill TODOs in config.yaml)

Run:
  python -m app.main run-once --dry-run --force [--client calix] [--carrier estes]
  python -m app.main daemon
  python -m app.main assist saia --client calix
Saia on a server: xvfb-run -a python -m app.main daemon
