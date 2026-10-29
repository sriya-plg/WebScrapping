"""Application settings and built-in carrier behavior profiles.

Carrier URLs, credentials, active flags, processing frequencies and delays are read from the
backend API. Only stable browser selectors and response/mapping behavior live in Python code.
"""
from __future__ import annotations

import logging
from pathlib import Path
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)
# Well-known canonical fields. Extra names are allowed and stored on TrackingResult.
COMMON_FIELDS = {
    "reference", "status", "pickup_date", "delivery_date", "eta",
    "origin", "destination", "pieces", "weight",
    "service_level", "pod_name", "pod_datetime",
}
EVENT_FIELDS = {
    "timestamp", "description", "location", "status",
    "code", "timezone", "lat", "long",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrowserConfig(_Strict):
    provider: Literal["playwright_stealth", "patchright"] = "playwright_stealth"
    browser_mode: Literal["playwright_stealth", "patchright"] | None = None
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
    """Stable browser selectors, response correlation, pacing, and JSON mapping behavior.

    The tracking URL itself is overlaid from the backend record at runtime.
    """
    tracking_url: str | None = None      # default: trackingUrl from the backend record
    input_selector: str | None = None    # default: auto-detect the search box
    submit_selector: str | None = None   # button to click; BatchScraper never relies on Enter
    response_regex: str | None = None    # default: any same-site JSON response containing the reference
    response_url_pattern: str | None = None
    # Regex/substring of the richer tracking API. When both the summary and details
    # endpoints fire (e.g. Saia find-by-pro vs find-full-details), the preferred URL
    # wins per PRO. If it never arrives, the other matching responses are still used.
    response_url_prefer: str | None = None
    expected_response_shape: Literal["single_batch", "per_pro"] = "per_pro"
    response_pro_path: str = "proNumber"
    input_mode: Literal["multiline", "delimited"] = "multiline"
    delimiter: str = "\n"
    batch_size: int = 1  # PROs per tracking submit
    strip_spaces_dashes: bool = True
    strip_leading_zeros: bool = False
    action_min_delay_s: float = 0.4
    action_max_delay_s: float = 1.2
    typing_delay_ms: float = 0.0         # per-character typing delay in ms (0 = instant fill)
    inter_pro_delay_s: float = 0.0       # pause between typing individual PRO numbers in seconds
    pre_submit_delay_s: float = 0.0      # pause before submitting tracking form in seconds
    inter_batch_delay_s: float = 0.0     # pause between separate batches in seconds
    idle_window_s: float = 2.0
    login: dict[str, Any] | None = None
    match_ref: bool = True               # require the captured JSON to contain the reference we searched
    response_timeout_s: float = 30
    settle_s: float = 1.0
    challenge_wait_s: float = 60
    extra: dict[str, Any] = Field(default_factory=dict)  # adapter-specific settings (e.g. Saia login)

    @field_validator("batch_size")
    @classmethod
    def _positive_batch_size(cls, value: int) -> int:
        if value < 1:
            raise ValueError("batch_size must be at least 1")
        return value

    @field_validator("action_max_delay_s")
    @classmethod
    def _delay_order(cls, value: float, info):
        minimum = info.data.get("action_min_delay_s", 0)
        if value < minimum:
            raise ValueError("action_max_delay_s must be >= action_min_delay_s")
        return value


def _default_activity_payload_map() -> dict[str, str]:
    return {
        "housebill": "ctx.housebill",
        "proNumber": "ctx.pro_number",
        "carrierCode": "ctx.carrier_code",
        "serviceLevel": "service_level",
        "billTo": "ctx.bill_to",
        "mlStatus": "ctx.ml_status",
        "mlLog": "ctx.ml_log",
        "mlStartDateTime": "ctx.ml_start",
        "mlEndDateTime": "ctx.ml_end",
        "mlProcessTime": "ctx.ml_process_time",
        "mlError": "ctx.ml_error",
        "reference": "reference",
        "podName": "pod_name",
        "podDateTime": "pod_datetime",
    }


def _default_activity_events_map() -> dict[str, str]:
    return {
        "code": "code|status",
        "description": "description",
        "eventDateTime": "timestamp",
        "timezone": "timezone",
        "lat": "lat",
        "long": "long",
    }


class ActivityConfig(_Strict):
    """AddCarrierActivity: 2=processing, 3=successful scrape, 4=failed/not found.

    payload_map / events_map turn TrackingResult + context into the backend body.
    """
    ml_status_default: int = 4
    ml_status_by_outcome: dict[str, int] = Field(default_factory=lambda: {
        "ok": 3,
        "not_found": 4,
        "no_response": 4,
        "map_failed": 4,
        "error": 4,
    })
    payload_map: dict[str, str] = Field(default_factory=_default_activity_payload_map)
    events_map: dict[str, str] = Field(default_factory=_default_activity_events_map)
    preserve_event_offsets: bool = False
    preserve_pod_datetime: bool = False


class MappingConfig(_Strict):
    """raw carrier JSON -> common schema. Explicit `fields` win; `auto` fills the gaps.

    Field names should match TrackingResult (or an extra key mapped into the backend payload).
    Dotted paths use `|` for first-non-empty fallbacks so carrier response changes are localized
    to this built-in Python profile.
    """
    auto: bool = True
    fields: dict[str, str] = Field(default_factory=dict)  # canonical field -> dotted path ("a.b|c.d" = first non-empty)
    compose: dict[str, str] = Field(default_factory=dict)  # field -> "{path} {path}" template (date+time, city+state)
    status_map: dict[str, str] = Field(default_factory=dict)  # carrier status text -> common status
    events: dict[str, Any] | None = None  # {path, fields, compose}

    @field_validator("fields")
    @classmethod
    def _fields(cls, v):
        if not isinstance(v, dict):
            raise ValueError("fields must be a mapping of name -> dotted path")
        return v

    @field_validator("events")
    @classmethod
    def _events(cls, v):
        if v is None:
            return v
        flds = v.get("fields") or {}
        unknown = set(flds) - EVENT_FIELDS
        if unknown:
            # Extra event keys are kept on TrackingEvent (extra=allow).
            logger.debug("carrier profile events.fields extra keys: %s", sorted(unknown))
        if "path" in v and not v["path"]:
            raise ValueError("events.path must be a dotted JSON path")
        return v


class CarrierSpec(_Strict):
    code: str
    client: str
    name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    limits: Limits = Field(default_factory=Limits)
    block_markers: list[str] = Field(default_factory=list)
    site: SiteConfig = Field(default_factory=SiteConfig)
    mapping: MappingConfig = Field(default_factory=MappingConfig)
    activity: ActivityConfig = Field(default_factory=ActivityConfig)


class BackendSettings(_Strict):
    mode: Literal["http"] = "http"
    base_url: str = ""
    token_env: str = "TRACKER_API_TOKEN"
    carrier_activity_path: str = "/api/orderentry/AddCarrierActivity"
    carrier_activity_auth: bool = False
    carrier_config_path: str = "/api/orderentry/GetCarrierConfigurationDetails"
    shipments_path: str = "/api/orderentry/GetCarrierShipmentDetails"
    carrier_config_method: Literal["GET", "POST"] = "GET"
    timeout_seconds: float = 30
    retry_attempts: int = 3
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    credential_resolver: str = "identity"


class ScheduleSettings(_Strict):
    interval_hours: float = 6
    jitter_minutes: int = 25
    seed: str = "tracker"


class AlertSettings(_Strict):
    webhook_url: str | None = None
    dedupe_hours: int = 6


class AppSettings(_Strict):
    """Application-only settings. Carrier URLs, credentials and cadence come from the backend API."""
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    logs_dir: str = "app/runtime/logs"
    browser_state_dir: str = "app/runtime/browser"
    debug_dir: str = "app/runtime/debug"
    backend: BackendSettings = Field(default_factory=BackendSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    dry_run: bool = False
    post_errors_to_tms: bool = False


def load_settings(path: str | Path | None = None) -> AppSettings:
    """Load non-carrier settings from environment variables; no external YAML config."""
    backend = BackendSettings(
        mode="http",
        base_url=os.environ.get("TRACKER_BASE_URL", "https://plgtst.pegasuslogistics.com"),
        token_env=os.environ.get("TRACKER_TOKEN_ENV", "TRACKER_API_TOKEN"),
        carrier_config_path=os.environ.get("CARRIER_CONFIG_PATH", "/api/orderentry/GetCarrierConfigurationDetails"),
        shipments_path=os.environ.get("SHIPMENTS_PATH", "/api/orderentry/GetCarrierShipmentDetails"),
        carrier_config_method=os.environ.get("CARRIER_CONFIG_METHOD", "GET"),
        carrier_activity_path=os.environ.get("CARRIER_ACTIVITY_PATH", "/api/orderentry/AddCarrierActivity"),
        carrier_activity_auth=os.environ.get("CARRIER_ACTIVITY_AUTH", "false").lower() in {"1", "true", "yes"},
        timeout_seconds=float(os.environ.get("BACKEND_TIMEOUT_SECONDS", "30")),
        retry_attempts=int(os.environ.get("BACKEND_RETRY_ATTEMPTS", "3")),
        auth_header=os.environ.get("BACKEND_AUTH_HEADER", "Authorization"),
        auth_scheme=os.environ.get("BACKEND_AUTH_SCHEME", "Bearer"),
        credential_resolver=os.environ.get("CREDENTIAL_RESOLVER", "identity"),
    )
    return AppSettings(
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        log_format=os.environ.get("LOG_FORMAT", "console"),
        logs_dir=os.environ.get("LOGS_DIR", "app/runtime/logs"),
        browser_state_dir=os.environ.get("BROWSER_STATE_DIR", "app/runtime/browser"),
        debug_dir=os.environ.get("SCRAPER_DEBUG_DIR", "app/runtime/debug"),
        backend=backend,
        schedule=ScheduleSettings(
            interval_hours=float(os.environ.get("RUN_INTERVAL_HOURS", "3")),
            jitter_minutes=0,
        ),
        alerts=AlertSettings(webhook_url=os.environ.get("ALERT_WEBHOOK_URL") or None),
        dry_run=os.environ.get("DRY_RUN", "false").lower() in {"1", "true", "yes"},
        post_errors_to_tms=os.environ.get("POST_ERRORS_TO_TMS", "false").lower() in {"1", "true", "yes"},
    )


class SpecResolver:
    """Resolve a carrier's code to built-in site behavior, overlaying API-owned runtime fields."""

    def __init__(self):
        from app.carrier_profiles import CARRIER_PROFILES
        self._profiles = CARRIER_PROFILES
        self._by_code: dict[str, CarrierSpec] = {}
        self._specs: dict[tuple[str, str], CarrierSpec] = {}
        self.errors: list[str] = []
        for code, raw in self._profiles.items():
            try:
                spec = CarrierSpec(**raw)
                for alias in [code, *spec.aliases]:
                    self._by_code[alias.upper()] = spec
                    self._specs[(spec.client.upper(), alias.upper())] = spec
            except Exception as exc:
                self.errors.append(f"built-in profile {code}: {type(exc).__name__}: {exc}")

    def resolve(self, bill_to: str, code: str, config: Any | None = None) -> CarrierSpec | None:
        spec = self._by_code.get(str(code).upper())
        if spec is None:
            return None
        site_updates: dict[str, Any] = {}
        if config is not None:
            tracking_url = getattr(config, "tracking_url", None)
            if tracking_url:
                site_updates["tracking_url"] = tracking_url
            delay = getattr(config, "processing_delay", None)
            if delay is not None and float(delay) > 0:
                site_updates["inter_pro_delay_s"] = max(spec.site.inter_pro_delay_s, float(delay))
        site = spec.site.model_copy(update=site_updates) if site_updates else spec.site
        return spec.model_copy(update={"site": site})

    def clients(self) -> list[str]:
        return sorted({spec.client for spec in self._by_code.values()})

    def carriers(self, client: str) -> list[str]:
        return sorted({spec.code for spec in self._by_code.values() if spec.client.upper() == client.upper()})


def load_resolver(clients_dir: str | Path | None = None) -> SpecResolver:
    """Compatibility entry point; no local client/carrier config files are read."""
    return SpecResolver()
