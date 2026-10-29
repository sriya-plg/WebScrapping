# Codebase guide and troubleshooting

## Runtime map

- `app/main.py`: CLI and composition root.
- `app/settings.py`: environment-based operational settings and typed profile models.
- `app/carrier_profiles.py`: stable carrier browser selectors, response capture patterns, and JSON mappings. No runtime URLs, credentials, or schedules belong here.
- `app/backend/client.py`: HTTP calls to carrier configuration, shipment intake, and AddCarrierActivity APIs.
- `app/core/intake.py`: active carrier intake and shipment deduplication within one API response only.
- `app/core/scraper.py`: Playwright/Patchright form interaction, response listener coordination, batch correlation, CAPTCHA/challenge classification.
- `app/core/mapper.py`: raw JSON to canonical tracking fields and event normalization.
- `app/core/carrier_activity.py`: payload construction and delivery to the backend.
- `app/browser/playwright_provider.py`: browser lifecycle, session storage state, and network listeners.
- `app/runtime/`: generated logs, diagnostics, and browser session files (not committed).

## Important invariants

1. Backend API values override any default and provide tracking URLs, credentials, active state, processing cadence, and delays.
2. Browser listeners capture JSON network responses; do not replace this with HTML-only scraping.
3. Every extracted result is submitted to the backend contract; the backend decides whether it is new, changed, or already processed.
4. There is no database or local persistent shipment deduplication.
5. A CAPTCHA challenge is not the same as a not-found shipment.
6. Never log plaintext credentials or put them in carrier profiles.

## Troubleshooting

| Symptom | Likely cause | Next step |
|---|---|---|
| Carrier profile not found | Backend carrier code has no built-in profile/alias | Add the stable carrier interaction profile to `app/carrier_profiles.py`; do not add tracking URL or schedule values there. |
| Tracking URL missing | Backend configuration omitted `trackingUrl` | Fix the backend carrier record. |
| Shipment marked missing | No matching network JSON response, or the carrier explicitly returned an empty/not-found response | Check the carrier response URL pattern and response-to-PRO path in the built-in profile; inspect sanitized diagnostics. |
| CAPTCHA / Cloudflare challenge | Carrier requires interactive verification or blocks the host/IP | Use `python -m app.main assist <carrier-code> --client <billTo>` to open a headed browser. If challenge remains, report the run as blocked rather than not-found. |
| 401/403 from backend | Missing or invalid API token | Check `TRACKER_API_TOKEN` and backend auth requirements. |
| Credential decryption failure | Resolver/key does not match backend encryption contract | Confirm the backend's encryption scheme. Use Fernet only if the backend explicitly uses Fernet; Base64 alone is not encryption. |
| Wrong event/status mapping | Carrier API payload changed | Update the relevant Python profile and test against a saved non-sensitive fixture. |
| Duplicate updates | Backend duplicate/change contract is not applying as expected | Inspect backend response/contract; do not add a local database to work around it. |

## Commands

```powershell
python -m app.main run-once --force --dry-run
python -m app.main run-once --force --carrier SAI001
python -m app.main daemon
python -m app.main assist SAI001 --client 134034
python -m app.tools.diagnose --carrier SAI001 --headed
```
