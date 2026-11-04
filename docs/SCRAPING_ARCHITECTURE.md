# Scraping architecture (refactored)

## Source of truth

The backend API is the source of truth for carrier records and shipment references. At runtime the app reads `trackingUrl`, `carrierCode`, `billTo`, username/password, active state, `processingFrequency`, and `processingDelay` from the backend. No local carrier YAML or JSON files define production settings.

The app has no database. It does not persist shipment hashes, processed status, pending queues, or last-run timestamps. The backend decides whether a result is new, changed, or already processed. A process-local frequency gate is only a scheduling convenience and resets on restart.

## Processing flow

```text
Carrier Configuration API
        |
        v
Active configuration filter
        |
        v
GetCarrierShipmentDetails API
        |
        v
Build per-carrier shipment batches
        |
        v
Sequential Playwright / Patchright browser session
  - open backend-provided trackingUrl
  - interact with carrier tracking form
  - attach response listeners before submit
  - capture matching carrier JSON responses
        |
        v
Normalize raw carrier JSON using built-in Python profile
        |
        v
Build AddCarrierActivity payload per housebill
        |
        v
Backend AddCarrierActivity API
  - backend decides new/changed/already processed
```

## Browser choice

Playwright is the default because page automation and JSON network-response interception are central requirements. Patchright remains available for profiles that need its Chromium patches. Crawl4AI and Browser Use provider integrations were removed because they were not a complete drop-in implementation of the current `BrowserProvider` and response-listener contract.

### Scrapling (opt-in, Saia)

Saia can run on Scrapling (`AsyncStealthySession`): patchright Chromium launched as a **persistent context** (`user_data_dir` under the browser state dir, so cookies/localStorage survive restarts) with extra stealth hardening (stealth launch args, canvas/WebRTC patching, ad-block list). Enable it with `SAIA_BROWSER_MODE=scrapling`; the profile default stays `patchright` so engine comparison changes exactly one variable at a time. The override is applied in `SpecResolver.resolve` for the canonical `SAIA` code only — Estes and Forward Air are unaffected, and an invalid value logs a warning and keeps the profile default.

The ScraplingProvider falls back to the plain patchright provider (today's production path) with a WARNING that carries the reason whenever: the `scrapling[fetchers]` extras are missing, the import fails, the profile is locked by another process (a lockfile guards `user_data_dir`; stale locks older than 24h are replaced), or the engine fails to launch. A second process (e.g. `diagnose`) therefore degrades to patchright instead of corrupting the profile. `solve_cloudflare` is deliberately not set — it only acts inside `engine.fetch()`, which the scraper never calls; the scraper drives a page from `engine.context` directly (same Playwright API as patchright).

### Resilience

- **Circuit breaker** per `(client, carrier, billTo)` group, from the profile's `limits.breaker_threshold` / `breaker_cooldown_s`. Because runs are one-shot processes on the deployment server, breaker state persists to `app/runtime/breakers.json` (set `BREAKER_STATE_FILE` to move it) so a cooldown survives restarts. `ChallengeError` outcomes record blocks; anything else records success. While open, the group is skipped and summarized as `skipped_breaker_open`.
- **Chunk retries**: a `TimeoutError` while waiting for the tracking JSON retries the chunk up to `limits.retries` times (re-navigating between attempts). Every other exception — including challenges — fails immediately.
- **Bot-check wait**: text markers, page title/URL, and 403/429 statuses enter the same wait-then-raise path — the scraper waits up to `site.challenge_wait_s` for an interstitial to clear (reloading once for status-only blocks) before raising `ChallengeError`. Challenges are never classified as not-found.

### Per-run outcome logging

Every run logs a `carrier run outcome` line and carries the same fields in the run summary: `started_at`, `engine_requested`, `engine_used` (a silent scrapling→patchright fallback shows up here), `batch_size`, `state_reused`, and `challenge_outcome` (`clean` / `checkbox_auto_passed` / `cloudflare_block` / `image_challenge` / `other_error`, most severe wins). Compare these over several days per engine before making the scrapling engine the profile default.

CAPTCHA handling is deliberately conservative: the scraper may click the checkbox and wait for an interactive verification when a visible browser is running, but it does not solve image challenges automatically. A challenge that cannot be completed is reported as a carrier-level error and should not be classified as a missing shipment.

## Carrier-specific behavior

`app/carrier_profiles.py` stores stable implementation details: selectors, response URL patterns, response-to-reference JSON paths, mapping rules, status normalization, and browser engine choice. Tracking URLs, credentials, active flags, processing frequencies, and processing delays must not be added to these profiles; those values come from the backend API.

When a carrier changes its UI or tracking API:

1. Run `python -m app.tools.diagnose --carrier <backend-carrier-code> --headed`.
2. Inspect browser/network diagnostics in `app/runtime/debug/`.
3. Update the corresponding profile in `app/carrier_profiles.py`.
4. Test with a known PRO and confirm that the captured JSON is correlated to the right shipment.
5. Confirm `AddCarrierActivity` payloads with a backend test environment before deploying.

## Runtime files

`app/runtime/logs/` contains logs; `app/runtime/debug/` contains screenshots and sanitized failure URLs; `app/runtime/browser/` may contain Playwright storage-state JSON to reuse browser cookies. Browser storage-state files are not a database, but they may contain authenticated session information and must be protected. There are no other local processing-state stores.

## Schedule semantics

`processingFrequency` is currently interpreted as minutes; zero means process on every daemon cycle. The frequency gate is in memory only, so it resets on restart. If the backend defines different units or zero semantics, change `_due()` in `app/core/runner.py` to match the backend contract.
