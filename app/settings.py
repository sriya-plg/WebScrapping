"""Settings + the client/carrier config loader.

Layout (one folder per client, everything for that client inside it):
  app/clients/<client>/client.yaml                       billTo codes + defaults for all its carriers
  app/clients/<client>/carriers/<carrier>/mapping.yaml   raw-JSON -> common-field mapping (+ optional overrides)
  app/clients/<client>/carriers/<carrier>/adapter.py     OPTIONAL: only for sites needing custom behaviour
  app/clients/<client>/carriers/<carrier>/hooks.py       OPTIONAL: post(payload, result, account)
Unknown YAML keys are rejected (typos fail loudly) and a broken carrier never stops the others from loading.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)
COMMON_FIELDS = {"status", "pickup_date", "delivery_date", "eta", "origin", "destination", "pieces", "weight"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrowserConfig(_Strict):
    provider: Literal["playwright_stealth", "patchright", "crawl4ai"] = "playwright_stealth"
    headless: bool = True
    channel: str | None = None          # e.g. "chrome" (recommended with patchright)
    locale: str = "en-US"
    timezone_id: str = "America/Chicago"
    viewport_w: int = 1366
    viewport_h: int = 820
    persist_state: bool = True          # reuse cookies between runs
    proxy: dict[str, str] | None = None
    nav_timeout_s: float = 45


class Limits(_Strict):
    max_concurrency: int = 1
    min_delay_s: float = 4
    max_delay_s: float = 11
    retries: int = 3
    backoff_base_s: float = 2
    backoff_cap_s: float = 60
    breaker_threshold: int = 3
    breaker_cooldown_s: int = 1800
    post_batch_size: int = 50


class SiteConfig(_Strict):
    """How to drive the site. EVERYTHING is optional: the generic adapter auto-detects it.
    Set a value only to override a wrong auto-detection."""
    tracking_url: str | None = None      # default: trackingUrl from the backend record
    input_selector: str | None = None    # default: auto-detect the search box
    submit_selector: str | None = None   # default: press Enter, then an auto-detected button
    response_regex: str | None = None    # default: any same-site JSON response containing the reference
    ref_type_select: str | None = None   # <select> for PRO/BOL if the site has one
    ref_type_map: dict[str, str] = {}
    transport: Literal["browser", "http"] = "browser"
    http_url_template: str | None = None  # transport=http: e.g. https://host/api/track/{ref}
    match_ref: bool = True               # require the captured JSON to contain the reference we searched
    response_timeout_s: float = 30
    settle_s: float = 1.0
    challenge_wait_s: float = 60
    extra: dict[str, Any] = {}           # adapter-specific settings (e.g. Saia login)


class MappingConfig(_Strict):
    """raw carrier JSON -> common schema. Explicit `fields` win; `auto` fills the gaps."""
    auto: bool = True
    fields: dict[str, str] = {}          # common field -> dotted path ("a.b|c.d" = first non-empty)
    status_map: dict[str, str] = {}      # carrier status text (any case) -> common status
    events: dict[str, Any] | None = None  # {path: "history", fields: {timestamp: "date", ...}}

    @field_validator("fields")
    @classmethod
    def _known(cls, v):
        bad = set(v) - COMMON_FIELDS
        if bad:
            raise ValueError(f"unknown common field(s) {sorted(bad)}; allowed: {sorted(COMMON_FIELDS)}")
        return v


class CarrierSpec(_Strict):
    code: str
    client: str
    name: str | None = None
    aliases: list[str] = []
    browser: BrowserConfig = BrowserConfig()
    limits: Limits = Limits()
    block_markers: list[str] = []
    site: SiteConfig = SiteConfig()
    mapping: MappingConfig = MappingConfig()
    pending: dict[str, Any] = {}         # client-level: how to call/parse the pending-refs API
    payload_map: dict[str, str] = {}     # client-level: common schema -> backend payload keys
    post_hook: str | None = None


class BackendSettings(_Strict):
    mode: Literal["file", "http"] = "file"
    base_url: str = ""
    token_env: str = "TRACKER_API_TOKEN"
    carriers_path: str = "/carriers"
    post_path: str = "/tracking/bulk"
    stub_dir: str = "stub"
    outbox_dir: str = "out"


class ScheduleSettings(_Strict):
    interval_minutes: int = 360
    jitter_minutes: int = 25
    seed: str = "tracker"


class AlertSettings(_Strict):
    webhook_url: str | None = None
    dedupe_hours: int = 6


class AppSettings(_Strict):
    log_level: str = "INFO"
    state_db: str = "state/tracker.db"
    state_dir: str = "state/browser"
    samples_dir: str = "state/samples"   # raw JSON saved when mapping fails / status unknown (to write mapping.yaml)
    clients_dir: str = "app/clients"
    backend: BackendSettings = BackendSettings()
    schedule: ScheduleSettings = ScheduleSettings()
    alerts: AlertSettings = AlertSettings()
    dry_run: bool = False


def load_settings(path: str | Path = "config/app.yaml") -> AppSettings:
    p = Path(path)
    return AppSettings(**(yaml.safe_load(p.read_text()) or {})) if p.exists() else AppSettings()


# ------------------------------------------------------------------------------------------ loader
_MAPPING_KEYS = {"auto", "fields", "status_map", "events"}


def _merge(a: dict, b: dict) -> dict:
    """Dicts merge recursively; everything else (lists, scalars) is replaced by the override."""
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class SpecResolver:
    """(billTo, carrierCode) -> CarrierSpec"""

    def __init__(self):
        self._by_bill_to: dict[str, str] = {}
        self._specs: dict[tuple[str, str], CarrierSpec] = {}
        self.errors: list[str] = []          # config problems, surfaced in logs; never fatal

    def resolve(self, bill_to: str, code: str) -> CarrierSpec | None:
        client = self._by_bill_to.get(bill_to.upper())
        return self._specs.get((client.upper(), code.upper())) if client else None

    def clients(self) -> list[str]:
        return sorted(set(self._by_bill_to.values()))

    def carriers(self, client: str) -> list[str]:
        return sorted({s.code for (c, _), s in self._specs.items() if c == client.upper()})


def load_resolver(clients_dir: str | Path) -> SpecResolver:
    res, root = SpecResolver(), Path(clients_dir)
    for cdir in sorted(p for p in root.glob("*") if (p / "client.yaml").exists()):
        key = cdir.name
        try:
            ccfg = yaml.safe_load((cdir / "client.yaml").read_text()) or {}
            defaults = ccfg.get("defaults", {})
            for b in ccfg.get("bill_to") or [key]:
                res._by_bill_to[b.upper()] = key
        except Exception as e:
            res.errors.append(f"{key}/client.yaml: {e}")
            continue
        for f in sorted((cdir / "carriers").glob("*/mapping.yaml")):
            code = f.parent.name.upper()
            try:
                raw = yaml.safe_load(f.read_text()) or {}
                mapping = {k: raw.pop(k) for k in list(raw) if k in _MAPPING_KEYS}
                merged = _merge(defaults, raw)
                merged["mapping"] = _merge(merged.get("mapping", {}), mapping)
                merged.update(code=code, client=key)
                if not merged.get("post_hook") and (f.parent / "hooks.py").exists():
                    merged["post_hook"] = f"app.clients.{key}.carriers.{f.parent.name}.hooks:post"
                spec = CarrierSpec(**merged)
                for k in (code, *spec.aliases):
                    res._specs[(key.upper(), k.upper())] = spec
            except Exception as e:      # one broken carrier must not take the others down
                res.errors.append(f"{key}/{f.parent.name}/mapping.yaml: {e}")
    for e in res.errors:
        logger.error("config error: %s", e)
    return res
