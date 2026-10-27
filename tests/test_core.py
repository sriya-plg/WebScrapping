import json
import pathlib
import time

import pytest

from app.backend.client import FileBackend
from app.core import registry
from app.core.intervention import InterventionQueue
from app.core.mapper import build_payload, get_path
from app.core.resilience import CircuitBreaker, NotFoundError, TransientError, retry
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


@pytest.mark.asyncio
async def test_daemon_runs_immediately_and_recovers_after_overrun(monkeypatch, caplog):
    import asyncio
    import signal

    from app import main

    monkeypatch.setenv("RUN_INTERVAL_HOURS", "0.000001")
    handlers = {}

    def capture_signal(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(main.signal, "signal", capture_signal)

    class Runner:
        def __init__(self):
            self.started = []
            self.active = 0
            self.max_active = 0

        async def run_once(self):
            self.started.append(asyncio.get_running_loop().time())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if len(self.started) == 1:
                    await asyncio.sleep(0.03)
                    raise RuntimeError("simulated run failure")
                handlers[signal.SIGINT](signal.SIGINT, None)
                await asyncio.sleep(0)
            finally:
                self.active -= 1

    runner = Runner()
    with caplog.at_level("INFO"):
        await main.daemon(None, runner, None)

    assert len(runner.started) == 2
    assert runner.started[1] - runner.started[0] < 0.5
    assert runner.max_active == 1
    assert "daemon started, running first job now" in caplog.text
    assert "run crashed; daemon continues" in caplog.text
    assert "run finished in" in caplog.text
    assert "next run at" in caplog.text
    assert "daemon sleeping for" in caplog.text
    assert "daemon stopped" in caplog.text


@pytest.mark.asyncio
async def test_daemon_wait_is_interrupted_by_sigterm(monkeypatch):
    import asyncio
    import signal

    from app import main

    monkeypatch.setenv("RUN_INTERVAL_HOURS", "3")
    handlers = {}

    def capture_signal(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(main.signal, "signal", capture_signal)

    class Runner:
        def __init__(self):
            self.calls = 0

        async def run_once(self):
            self.calls += 1
            asyncio.get_running_loop().call_later(
                0.01, handlers[signal.SIGTERM], signal.SIGTERM, None,
            )

    runner = Runner()
    started = asyncio.get_running_loop().time()
    await main.daemon(None, runner, None)

    assert runner.calls == 1
    assert asyncio.get_running_loop().time() - started < 1


def test_estes_normalize_from_fixture():
    from app.core.adapter import GenericAdapter
    raw = json.loads((ROOT / "tests/fixtures/estes_response.json").read_text())
    a = GenericAdapter(RES.resolve("CALIX", "ESTES"), ACCT, None)
    r = a.normalize(raw, "1234567890", "PRO")
    assert r.status == "DELIVERED" and r.delivered and r.weight == 1250.0 and r.pieces == 4
    assert r.events[0].description == "Delivered"
    p = build_payload(RES.resolve("CALIX", "ESTES"), r, ACCT, "run1")
    assert p["proNumber"] == "1234567890" and p["billTo"] == "CALIX" and p["source"] == "web-scrape"
    assert "raw" not in p


def test_get_path_fallbacks():
    assert get_path({"a": {"b": 1}}, "x|a.b") == 1
    assert get_path({"l": [{"k": 2}]}, "l.0.k") == 2


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
    sv = Services(s, FileBackend(s.backend), store, InterventionQueue(store, AlertSettings()),
                  provider_factory=lambda cfg, d: type("P", (), {"close": lambda self: _noop()})())
    mk = lambda: Runner(sv, specs)
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
    saia = RES.resolve("CALIX", "SAIA")
    assert s.client == "calix" and s.code == "ESTES"
    assert s.pending["endpoint"].startswith("/calix/")            # from client.yaml defaults
    assert saia.site.tracking_url.startswith("https://www.saia")
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


def test_run_file_logger_creates_per_run_file(tmp_path):
    import logging

    from app.logging_setup import run_file_logger

    test_logger = logging.getLogger("test_run")
    with run_file_logger(run_id="abc123test", logs_dir=tmp_path) as log_file:
        test_logger.info("testing per-run file logger")
        assert log_file.exists()
        assert "run_" in log_file.name
        assert "abc123test" in log_file.name

    content = log_file.read_text(encoding="utf-8")
    assert "testing per-run file logger" in content

