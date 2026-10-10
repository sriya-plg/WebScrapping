import json
import pathlib
import sqlite3

import pytest

from app.backend.client import FileBackend
from app.core import registry
from app.core.intervention import InterventionQueue
from app.core.mapper import build_payload, get_path
from app.core.resilience import CircuitBreaker, TransientError, retry
from app.core.runner import Runner
from app.core.scheduler import JitterSchedule
from app.core.state import StateStore
from app.models import CarrierAccount
from app.settings import AlertSettings, AppSettings, load_resolver

ROOT = pathlib.Path(__file__).resolve().parents[1]
RES = load_resolver(ROOT / "app/clients")
ACCT = CarrierAccount(id=1, carrierName="Estes", carrierCode="ESTES", billTo="CALIX")


def test_jitter_bounds_restart_safe_and_no_drift():
    s = JitterSchedule(0, 6 * 3600, 25 * 60)
    for k in range(200):
        assert abs(s.slot_time(k) - k * 6 * 3600) <= 25 * 60
    assert s.slot_time(7) == JitterSchedule(0, 6 * 3600, 25 * 60).slot_time(7)
    gaps = [s.slot_time(k + 1) - s.slot_time(k) for k in range(200)]
    assert min(gaps) >= 6 * 3600 - 50 * 60 and max(gaps) <= 6 * 3600 + 50 * 60 and len(set(gaps)) > 100
    now = 123456.0
    assert s.next_after(now) > now


def test_estes_normalize_from_fixture():
    from app.core.mapper import normalize
    raw = json.loads((ROOT / "tests/fixtures/estes_response.json").read_text())
    r = normalize(RES.resolve("CALIX", "ESTES"), raw, "1234567890", "PRO", ACCT)
    assert r.status == "DELIVERED" and r.delivered and r.weight == 1250.0 and r.pieces == 4
    assert r.events[0].description == "Delivered"
    p = build_payload(RES.resolve("CALIX", "ESTES"), r, ACCT, "run1")
    assert p["proNumber"] == "1234567890" and p["billTo"] == "CALIX" and p["source"] == "web-scrape"
    assert "raw" not in p


async def test_carrier_json_404_is_not_found_without_submit_retries():
    from app.browser.base import CapturedResponse
    from app.core.adapter import GenericAdapter
    from app.core.resilience import NotFoundError

    class Session:
        captures = 0
        async def wait_for_selector(self, *args, **kwargs): return True
        async def fill(self, *args, **kwargs): pass
        async def click(self, *args, **kwargs): pass
        async def capture_all(self, action, **kwargs):
            self.captures += 1
            await action()
            return [CapturedResponse("https://myestes-api.estes-express.com/shipmenttracking/history?pro=0",
                                     404, {"message": "not found"})]

    spec = load_resolver(ROOT / "app/clients").resolve("CALIX", "ESTES")
    spec.site.input_selector, spec.site.submit_selector = "#pro", "#submit"
    account = CarrierAccount(id=1, carrierName="Estes", carrierCode="ESTES", billTo="CALIX",
                             trackingUrl="https://www.estes-express.com/myestes/shipment-tracking/")
    adapter = GenericAdapter(spec, account, None)
    session = Session()
    adapter.session = session
    adapter._sel = {"input": "#pro", "submit": "#submit", "source": "config"}
    async def no_wait(_): pass
    adapter._wait_ready = no_wait
    with pytest.raises(NotFoundError):
        await adapter._search("0000000000", "PRO")
    assert session.captures == 1


def test_get_path_fallbacks():
    assert get_path({"a": {"b": 1}}, "x|a.b") == 1
    assert get_path({"l": [{"k": 2}]}, "l.0.k") == 2


async def test_browser_navigation_waits_for_response_commit(monkeypatch):
    from app.browser.playwright_provider import _PWSession

    class Page:
        def __init__(self): self.args = None
        def set_default_timeout(self, _): pass
        async def goto(self, url, **kwargs): self.args = (url, kwargs)

    page = Page()
    monkeypatch.setattr("app.browser.playwright_provider.random.uniform", lambda *_: 0)
    session = _PWSession(None, page, None, 45)
    await session.goto("https://carrier.example/track")
    assert page.args == ("https://carrier.example/track", {"wait_until": "commit"})


async def test_search_box_discovery_waits_for_client_render(monkeypatch):
    from app.core.adapter import GenericAdapter

    spec = RES.resolve("CALIX", "ESTES")
    spec.site.response_timeout_s = 1
    account = CarrierAccount(id=1, carrierName="Estes", carrierCode="ESTES", billTo="CALIX",
                             trackingUrl="https://www.estes-express.com/myestes/shipment-tracking/")
    adapter = GenericAdapter(spec, account, None)

    class Session:
        attempts = 0
        async def discover_search_box(self):
            self.attempts += 1
            return None if self.attempts < 3 else {"input": "#tracking", "submit": "#submit"}

    session = Session()
    adapter.session = session
    await adapter._resolve_selectors()
    assert adapter._sel["input"] == "#tracking"
    assert session.attempts == 3


def test_legacy_intervention_database_migrates_without_dropping_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE intervention(id INTEGER PRIMARY KEY, ts REAL, run_id TEXT, carrier TEXT, "
               "bill_to TEXT, reference TEXT, reason TEXT, resolved INTEGER DEFAULT 0)")
    db.execute("INSERT INTO intervention(ts, run_id, carrier, bill_to, reference, reason) "
               "VALUES(1, 'oldrun', 'ESTES', 'CALIX', '123', 'blocked')")
    db.commit(); db.close()

    store = StateStore(str(db_path))
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(intervention)")}
    assert "client" in columns
    assert store.db.execute("SELECT reference FROM intervention").fetchone() == ("123",)


async def test_retry_and_breaker():
    n = {"c": 0}

    async def flaky():
        n["c"] += 1
        if n["c"] < 3:
            raise TransientError()
        return "ok"
    assert await retry(flaky, attempts=3, base=0.001, cap=0.002) == "ok"
    t = [0.0]
    b = CircuitBreaker(2, 100, now=lambda: t[0])
    b.record_block(); assert b.allow()
    b.record_block(); assert not b.allow()
    t[0] = 101; assert b.allow()


async def test_partial_failure_and_idempotency(tmp_path):
    from app.core.adapter import CarrierAdapter
    from app.models import TrackingResult

    @registry.register("ESTES", client="calix")   # fake adapter for this test
    class Fake(CarrierAdapter):
        async def track(self, ref, ref_type):
            if ref == "0000000000":
                raise RuntimeError("boom")      # one bad PRO
            return json.loads((ROOT / "tests/fixtures/estes_response.json").read_text())

    s = AppSettings(state_db=":memory:", dry_run=False)
    s.backend.stub_dir, s.backend.outbox_dir = str(ROOT / "stub"), str(tmp_path / "out")
    specs = RES
    for c in ("ESTES",):
        v = RES.resolve("CALIX", c)
        v.limits.min_delay_s = v.limits.max_delay_s = 0
        v.limits.retries = 1
    store = StateStore(":memory:")
    from app.core.job import Services
    mk = lambda: Runner(Services(s, FileBackend(s.backend), store, InterventionQueue(store, AlertSettings()),
                        provider_factory=lambda cfg, d: type("P", (), {"close": lambda self: _noop()})()), specs)
    async def _noop(): return None
    m1 = await mk().run_once(force=True)
    assert m1["calix/ESTES"]["success"] == 1 and m1["calix/ESTES"]["failed"] == 1 and m1["calix/ESTES"]["posted"] == 1
    out = list((tmp_path / "out").rglob("*.json"))
    assert len(out) == 1 and len(json.loads(out[0].read_text())) == 1
    m2 = await mk().run_once(force=True)               # rerun: unchanged ref is not re-sent
    assert m2["calix/ESTES"]["unchanged"] == 1 and "posted" not in m2["calix/ESTES"]


def test_client_folder_resolution_and_discovery():
    RES = load_resolver(ROOT / "app/clients")
    s = RES.resolve("CALIX", "ESTES")
    assert s.client == "calix" and s.code == "ESTES"
    assert s.pending["endpoint"].startswith("/calix/")            # from client.yaml defaults
    assert s.site.tracking_url is None  # runtime trackingUrl comes from the carrier account
    assert s.post_hook == "app.clients.calix.carriers.estes.hooks:post"  # auto-detected hooks.py
    assert RES.resolve("calix", "exla") is s and RES.resolve("CALIX", "FWDA").code == "FORWARDAIR"  # aliases
    assert RES.carriers("calix") == ["ESTES", "FORWARDAIR", "SAIA"]
    assert RES.resolve("OTHERCO", "ESTES") is None
    registry._REG.clear(); registry.discover()
    assert registry.get_adapter_class("SAIA", "calix").__name__ == "SaiaAdapter"
    assert registry.get_adapter_class("FORWARDAIR", "calix").__name__ == "GenericAdapter"
    from app.models import TrackingResult
    p = build_payload(s, TrackingResult(reference="1", ref_type="PRO", carrier_code="ESTES", bill_to="CALIX",
                                        delivery_date="2026-10-01T10:00"), ACCT, "r")
    assert p["housebill"] == "1" and p["deliveryDate"] == "2026-10-01"
