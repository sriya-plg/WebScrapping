# Client and carrier architecture

## Source of truth

Client-to-carrier assignments, BillTo, account IDs, tracking URLs, credentials,
active state, and processing cadence are loaded from the backend API at runtime.
This application has no local client configuration files and introduces no database.

## Runtime flow

1. `backend/client.py` fetches carrier account configurations and shipment references.
2. `domain/job_context.py` builds a per-account `JobContext` from the backend record.
3. `settings.SpecResolver` resolves a reusable carrier profile by carrier code and
   overlays backend-owned runtime settings such as tracking URL and processing delay.
4. `carriers/adapter_registry.py` selects an adapter by carrier code only. It never
   selects an adapter by client name. Saia, Estes, and Forward Air can therefore be
   shared by any number of client accounts.
5. The existing `BatchScraper` retains the current Playwright/Patchright browser
   lifecycle, site-specific profile behavior, network-response capture, and retries.
6. `core/carrier_activity.py` maps the captured response to the canonical tracking
   result and builds the existing AddCarrierActivity payload. `billTo` is forced from
   the current backend account context immediately before posting.
7. `clients/registry.py` provides an optional policy hook for real client-specific
   payload differences. The default policy is a no-op; no class is needed per client.
8. The backend remains authoritative for duplicate/change decisions.

## Adding behavior

- Add `profile.py` and `adapter.py` under `carriers/<carrier>/` for declarative
   selectors, mapping, and carrier-specific interaction behavior. Register the
   profile in `carriers/registry.py` and the adapter in `adapter_registry.py`.
- Add a client policy only when a client has a verified business rule that cannot be
  represented by the backend configuration or the common payload contract. Register
  the policy against the runtime client key in `clients/registry.py`.
- Never hardcode a BillTo, client assignment, credential, or tracking URL in a carrier
  adapter. Do not log credentials.

## Important behavior

The Saia adapter does not guarantee automatic solving of visual CAPTCHA challenges;
existing challenge detection/manual-intervention behavior remains in force. Estes and
Forward Air keep their existing browser interaction and response-mapping behavior.
