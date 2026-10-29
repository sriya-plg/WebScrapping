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
