"""App settings (config/app.yaml) and per-carrier specs (config/carriers/*.yaml)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class BrowserConfig(BaseModel):
    provider: Literal["playwright_stealth", "patchright", "crawl4ai"] = "playwright_stealth"
    headless: bool = True
    channel: str | None = None          # e.g. "chrome" (recommended with patchright)
    locale: str = "en-US"
    timezone_id: str = "America/Chicago"
    viewport_w: int = 1366
    viewport_h: int = 820
    persist_state: bool = True          # reuse cookies (cf_clearance etc.) between runs
    proxy: dict[str, str] | None = None
    nav_timeout_s: float = 45


class Limits(BaseModel):
    max_concurrency: int = 1
    min_delay_s: float = 4
    max_delay_s: float = 11
    retries: int = 3
    backoff_base_s: float = 2
    backoff_cap_s: float = 60
    breaker_threshold: int = 3
    breaker_cooldown_s: int = 1800
    post_batch_size: int = 50


class NormalizeConfig(BaseModel):
    fields: dict[str, str] = {}          # common field -> dotted path in raw JSON ("a.b|fallback.c")
    status_map: dict[str, str] = {}      # lower-cased carrier status text -> common status
    events: dict[str, Any] | None = None  # {path: "history", fields: {timestamp: "date", ...}}


class CarrierSpec(BaseModel):
    code: str
    client: str = ""               # set by the loader (folder name under app/clients)
    name: str | None = None
    aliases: list[str] = []
    browser: BrowserConfig = BrowserConfig()
    limits: Limits = Limits()
    block_markers: list[str] = []
    pending: dict[str, Any] = {}         # how to call/parse the pending-refs API for this carrier
    adapter: dict[str, Any] = {}         # adapter-specific: urls, selectors, response patterns
    normalize: NormalizeConfig = NormalizeConfig()
    payload_map: dict[str, str] = {}     # backend key -> path in common schema ("=literal" allowed)
    post_hook: str | None = None         # "package.module:function"


class BackendSettings(BaseModel):
    mode: Literal["file", "http"] = "file"
    base_url: str = ""
    token_env: str = "TRACKER_API_TOKEN"
    carriers_path: str = "/carriers"
    post_path: str = "/tracking/bulk"
    stub_dir: str = "stub"
    outbox_dir: str = "out"


class ScheduleSettings(BaseModel):
    interval_minutes: int = 360
    jitter_minutes: int = 25
    seed: str = "tracker"


class AlertSettings(BaseModel):
    webhook_url: str | None = None
    dedupe_hours: int = 6


class AppSettings(BaseModel):
    log_level: str = "INFO"
    state_db: str = "state/tracker.db"
    state_dir: str = "state/browser"
    clients_dir: str = "app/clients"           # one folder per client: client.yaml + carriers/<carrier>/
    backend: BackendSettings = BackendSettings()
    schedule: ScheduleSettings = ScheduleSettings()
    alerts: AlertSettings = AlertSettings()
    dry_run: bool = False


def load_settings(path: str | Path = "config/app.yaml") -> AppSettings:
    p = Path(path)
    return AppSettings(**(yaml.safe_load(p.read_text()) or {})) if p.exists() else AppSettings()


def _merge(a: dict, b: dict) -> dict:
    """Dicts merge recursively; everything else (lists, scalars) is replaced by the override."""
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class SpecResolver:
    """(billTo, carrierCode) -> CarrierSpec, from  app/clients/<client>/client.yaml (defaults)
    merged under  app/clients/<client>/carriers/<carrier>/config.yaml.  Folder name = carrier code."""

    def __init__(self):
        self._by_bill_to: dict[str, str] = {}                 # BILLTO -> client key
        self._specs: dict[tuple[str, str], CarrierSpec] = {}  # (CLIENTKEY, CODE|ALIAS) -> spec

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
        ccfg = yaml.safe_load((cdir / "client.yaml").read_text()) or {}
        key = cdir.name
        for b in ccfg.get("bill_to") or [key]:
            res._by_bill_to[b.upper()] = key
        for f in sorted((cdir / "carriers").glob("*/config.yaml")):
            code = f.parent.name.upper()
            raw = _merge(ccfg.get("defaults", {}), yaml.safe_load(f.read_text()) or {})
            raw.update(code=code, client=key)
            if not raw.get("post_hook") and (f.parent / "hooks.py").exists():
                raw["post_hook"] = f"app.clients.{key}.carriers.{f.parent.name}.hooks:post"
            spec = CarrierSpec(**raw)
            for k in (code, *spec.aliases):
                res._specs[(key.upper(), k.upper())] = spec
    return res
