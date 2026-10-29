# Backend-driven freight tracking scraper

The application fetches carrier configuration and shipment references from the backend, drives each carrier's tracking website with Playwright/Patchright, captures the website's JSON network responses, maps the payload, and sends the result to the backend's `AddCarrierActivity` API.

## Architecture

1. **Carrier configuration API** supplies `carrierCode`, `trackingUrl`, `billTo`, credentials, `isActive`, `processingFrequency`, and `processingDelay`.
2. **Shipment API** supplies housebills, PRO numbers, and metadata.
3. **Built-in carrier profiles** in `app/carrier_profiles.py` hold only stable carrier-specific browser selectors, network response patterns, mapping rules, and safe interaction pacing. They do not override URLs or schedule values from the API.
4. **Playwright/Patchright** opens the real tracking page and listens for JSON network responses. Carrier groups are processed sequentially so only one browser lifecycle runs at a time. It does not replace network response capture with HTML scraping.
5. **Mapping and delivery** normalize the captured JSON and post one `AddCarrierActivity` update per shipment row. The backend decides whether a result is new, changed, or already processed.

There is **no database** and no local shipment deduplication. Processing-frequency bookkeeping is in memory only and resets on restart. The backend remains authoritative for result status and duplicate detection. Browser storage-state JSON files may be retained under `app/runtime/browser/` so verified sessions/cookies can be reused; these are browser session files, not a database. Keep that directory private because it may contain authenticated browser state.

## Browser engine choice

- **Playwright stealth** is the default: it supports page automation and network response listeners used by the scraper.
- **Patchright** is retained for Saia and Forward Air profiles where the existing configuration calls for it.
- **Crawl4AI is not used**: the former provider was only a stub and does not implement the current browser-session/network-listener contract.
- CAPTCHA image challenges are not automatically bypassed. Run headed and use the `assist` command to complete a challenge manually, or let the scraper report a `ChallengeError`.

## Configure environment

Copy `.env.example` to `.env` and load it into the process environment using your deployment's secret manager or service configuration. The application does not read an external YAML carrier config.

Required/commonly used variables:

- `TRACKER_BASE_URL`: backend base URL.
- `TRACKER_API_TOKEN`: optional bearer token if the backend requires authentication.
- `CARRIER_CONFIG_METHOD`: `GET` by default; use `POST` only if the backend contract requires it.
- `CARRIER_CONFIG_PATH`, `SHIPMENTS_PATH`, `CARRIER_ACTIVITY_PATH`: API paths if different from defaults.
- `CARRIER_CREDENTIALS_KEY`: Fernet key only when carrier passwords returned by the backend are Fernet-encrypted. Set `CREDENTIAL_RESOLVER=fernet` explicitly when that matches the backend contract. Do not treat Base64 as encryption or guess the encoding.
- `BROWSER_HEADLESS`: `true` or `false` to override the profile's browser mode.
- `RUN_INTERVAL_HOURS`: daemon cadence (default 3 hours).
- `RUN_TIMEOUT_HOURS`: optional per-cycle timeout.
- `LOG_LEVEL`, `LOG_FORMAT`, `LOGS_DIR`, `BROWSER_STATE_DIR`, `SCRAPER_DEBUG_DIR`: operational settings.

Carrier `processingFrequency` is interpreted as **minutes** (for example, `1439` is approximately one day); `0` means run on each daemon cycle. Because there is no persistent local scheduler, a restart resets this in-memory timing gate. `--force` ignores the gate. Confirm the units with the backend contract if they differ.

## Run

```powershell
# One scrape cycle; `--force` ignores the process-local frequency gate
python -m app.main run-once --force

# Restrict to a carrier (backend carrier code or a built-in alias)
python -m app.main run-once --force --carrier SAI001
python -m app.main run-once --force --carrier ESTE03
python -m app.main run-once --force --carrier FORW-LTL

# Do not send AddCarrierActivity updates (still performs intake and scraping)
python -m app.main run-once --force --dry-run

# Repeat cycles at RUN_INTERVAL_HOURS
python -m app.main daemon

# Open a carrier tracking URL in a headed browser to handle a challenge
python -m app.main assist SAI001 --client 134034

# Browser lifecycle diagnostic against the backend-provided tracking URL
python -m app.tools.diagnose --carrier SAI001 --headed
```

Install browser dependencies after installing requirements:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Patchright may require its own browser install according to the installed Patchright version.

## Logs and runtime files

Runtime logs, screenshots, sanitized failure URLs, and browser storage state are under `app/runtime/` by default. No database, local queue, local processed-shipment list, or local carrier configuration is created.

## Add support for a carrier

Add a Python profile to `app/carrier_profiles.py` containing the carrier's selectors, network response URL pattern, response-to-PRO path, mapping, and status normalization. Do not hardcode `trackingUrl`, credentials, active state, or processing cadence there; those remain backend-owned. Test with a known shipment and inspect the captured network response before changing mapping rules.
