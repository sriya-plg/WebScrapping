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

## Backend shipment intake

At each `run-once` start, `HttpBackend` reads carrier configurations, filters inactive
entries, then requests pending shipments once for each configured `(billTo, carrierCode)`.
The existing runner hands those references to the existing carrier adapters:

`6-hour jittered scheduler -> carrier configuration API -> shipment API per carrier -> BatchScraper -> raw response batches`

Set `backend.mode: http` and configure `backend.base_url`, `carrier_config_path`,
`shipments_path`, `carrier_config_method`, `timeout_seconds`, `retry_attempts`,
`auth_header`, and `auth_scheme` in `config/app.yaml`. `TRACKER_BASE_URL` can
override the configured base URL. The configuration endpoint
currently defaults to `GET`; shipment requests use `POST` with `billTo` and
`carrierCode`. `TRACKER_API_TOKEN` is optional and is read only when configured.
Use `.env.example` as a reference and export the variables in the process environment.
Backend passwords are held as `SecretStr` and passed through the placeholder
`IdentityCredentialResolver`; replace its TODO once the encryption scheme is supplied.

Run one intake and scraper cycle with `python -m app.main run-once --force`.
Run the separable recurring scheduler with `python -m app.main daemon`. Runs are
serialized in-process. The default cadence is 6 hours with ±25 minutes of jitter.

## Browser response batching (Phase 2)

The HTTP intake path now groups active tracking jobs by client and carrier. `BatchScraper`
opens one browser session per carrier, fills the configured input with batches of PROs,
clicks the configured button once, and captures JSON responses from the page's network
response events. It maps responses back by the configured `response_pro_path`, retries
missing PROs once individually, and returns raw JSON in each `ScrapeBatchResult`.
`Runner.run_once()` returns those under `scrape_results`; mapping and backend posting are
not part of this phase. File-backed runs retain the existing adapter path.

Carrier behavior lives in each `mapping.yaml` under `site`: selectors, `input_mode`,
`delimiter`, `batch_size`, `response_url_pattern`, `expected_response_shape`,
`response_pro_path`, normalization flags, timeouts, pacing, and optional login selectors.
Login config uses `enabled`, `required`, `url`, and `selectors` (`username`, `password`,
`submit`, optional `success`); leave it disabled until the carrier's login flow is verified.
Use `[]` in JSON paths to expand arrays (for example, `data[].proNumber`). Set
`browser.browser_mode` per carrier; Saia uses `patchright` for its undetected browser mode.
Errors are isolated to a carrier batch, and diagnostics save a screenshot plus a
sanitized URL file under `debug_dir`.

To add a carrier, create its client/carrier folder and mapping YAML, then fill in the
clearly marked selector and response-pattern TODO values after observing the carrier
page's browser network traffic. No core-code carrier-name branch is needed.

## Diagnostics & Troubleshooting (Deployment & Standalone Tool)

A standalone diagnostic tool is available to test the browser lifecycle (steps 1 to 7) without intake, PRO numbers, or TMS posting:

```powershell
# Run diagnostics for Forward Air:
python -m app.tools.diagnose --carrier FORWARDAIR

# Run diagnostics with headed browser to see the GUI:
python -m app.tools.diagnose --carrier FORWARDAIR --headed

# Run diagnostics for Saia with custom URL override:
python -m app.tools.diagnose --carrier SAIA --url https://www.saia.com
```

### Running on Windows Server

To run diagnostics under the exact same Windows service/task user account:

1. **Interactive Run under Service Account**:
   ```cmd
   runas /user:DOMAIN\ServiceAccount "powershell -NoExit -Command cd C:\projects\WebScrapping; python -m app.tools.diagnose --carrier FORWARDAIR"
   ```

2. **Administrator-less Scheduled Task**:
   If the job runs via Windows Task Scheduler without interactive login:
   ```cmd
   schtasks /create /tn "DiagnoseScraper" /tr "C:\projects\WebScrapping\.venv\Scripts\python.exe -m app.tools.diagnose --carrier FORWARDAIR" /sc once /st 12:00 /ru "DOMAIN\ServiceAccount" /rp "YourPassword"
   schtasks /run /tn "DiagnoseScraper"
   ```
   Inspect results in `logs/diagnostics/diagnose_forwardair_<timestamp>.log`.

### Diagnostic Environment Variables

- `SCRAPER_DEBUG=1`: Enables detailed browser console, request failure reasons, page errors, responses >= 400, and enables `DEBUG=pw:api,pw:browser` driver logs. Default: `0`.
- `SCRAPER_TRACE=1`: Captures full Playwright execution traces saved as `trace.zip` inside `logs/diagnostics/<run_id>/...`. View with `npx playwright show-trace trace.zip`. Default: `0`.
- `POST_ERRORS_TO_TMS=true`: Controls whether scraper infrastructure failures are reported to TMS (`AddCarrierActivity`). Default: `false` (skips TMS post with warning).
- `BROWSER_HEADLESS=true/false`: Global override for browser headless mode across all carriers. If unset, automatically defaults to `true` when running in headless environments (e.g. without DISPLAY on POSIX or non-interactive Windows services).

