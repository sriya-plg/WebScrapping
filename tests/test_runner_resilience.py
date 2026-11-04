"""Runner circuit breaker: persisted per-carrier cooldown + engine outcome fields in the summary."""
import asyncio
import json
from pathlib import Path

from app.browser.base import BrowserProvider, BrowserSession, CapturedResponse
from app.core.runner import Runner
from app.models import CarrierConfig, ShipmentRef
from app.settings import AppSettings, CarrierSpec


class FakeSession(BrowserSession):
    def __init__(self, responses_by_input=None, blocked=False):
        self.responses_by_input = responses_by_input or {}
        self.blocked = blocked
        self.input_value = ""
        self.urls = []
        self.clicks = 0
        self.engine_name = "patchright"
        self.state_reused = False

    async def goto(self, url): self.urls.append(url)
    async def fill(self, selector, value, human=True, frame=None): self.input_value = value
    async def set_input_value(self, selector, value, frame=None): self.input_value = value
    async def type_text(self, selector, text, delay_ms=0, clear=False, frame=None):
        self.input_value = (text if clear else self.input_value + text)
    async def get_value(self, selector, frame=None): return self.input_value
    async def click(self, selector, frame=None, force=False): self.clicks += 1
    async def press(self, selector, key, frame=None): pass
    async def select_option(self, selector, value, frame=None): pass
    async def wait_for_selector(self, selector, timeout_s=20, frame=None): return True
    async def is_visible(self, selector, frame=None): return False
    async def evaluate(self, expression, arg=None): return None
    async def discover_search_box(self): return None
    async def capture_all(self, action, timeout_s=30, settle_s=1, until=None):
        await action()
        return self.responses_by_input.get(self.input_value, [])
    async def looks_blocked(self, markers=None): return self.blocked
    async def save_debug(self, path): return path
    def current_url(self): return self.urls[-1] if self.urls else "https://www.saia.com/track"
    async def save_state(self): pass
    async def close(self): pass


class FakeProvider(BrowserProvider):
    def __init__(self, session):
        self.session = session

    async def open_session(self, key):
        return self.session

    async def close(self): pass


class FakeQueue:
    def __init__(self):
        self.alerts = []

    async def raise_(self, run_id, client, carrier, bill_to, refs, reason, url=None):
        self.alerts.append((client, carrier, reason))


class FakeBackend:
    def __init__(self, config, shipments):
        self.config, self.shipments = config, shipments

    async def get_carrier_configs(self): return [self.config]

    async def get_shipments(self, bill_to, carrier_code): return self.shipments


class FakeResolver:
    """Resolves only SAIA to a fast-test spec (no waits, no posting)."""

    def __init__(self, spec):
        self.spec = spec
        self._specs = {}  # environment report iterates this

    def resolve(self, bill_to, code, config=None):
        if str(code).upper() in ("SAIA", "SAI001"):
            site = self.spec.site.model_copy(
                update={"tracking_url": getattr(config, "tracking_url", None)})
            return self.spec.model_copy(update={"site": site})
        return None


def saia_spec(breaker_threshold=2, cooldown_s=3600):
    return CarrierSpec(code="SAIA", client="CALIX NETWORKS", aliases=["SAI001"],
                       block_markers=["just a moment"],
                       limits={"retries": 0, "max_concurrency": 1, "min_delay_s": 0,
                               "max_delay_s": 0, "breaker_threshold": breaker_threshold,
                               "breaker_cooldown_s": cooldown_s},
                       site={"tracking_url": "https://www.saia.com/track",
                             "input_selector": "#trackingNumbers",
                             "submit_selector": "#trackBtn", "batch_size": 2,
                             "response_url_pattern": "find-by-pro-number",
                             "expected_response_shape": "single_batch",
                             "response_pro_path": "[].proNumber",
                             "action_min_delay_s": 0, "action_max_delay_s": 0,
                             "idle_window_s": 0.01, "challenge_wait_s": 0.05,
                             "extra": {"pro_input_digits": 11,
                                       "retry_missing_individually": False}},
                       mapping={"auto": True, "fields": {}, "status_map": {}},
                       activity={})


class FakeSv:
    def __init__(self, settings, backend, provider, queue):
        self.settings = settings
        self.backend = backend
        self.provider_factory = lambda browser_cfg, state_dir: provider
        self.queue = queue


def saia_payload(pro):
    return [{"referenceNumbers": {"proNumber": pro}, "shipment": {"status": "In Transit"}}]


def run_once(tmp_path, session, breaker_threshold=2):
    config = CarrierConfig(id=1, carrierName="Saia", carrierCode="SAI001",
                           trackingUrl="https://www.saia.com/track", billTo=134034,
                           customerName="CALIX NETWORKS", isActive=True)
    shipments = [ShipmentRef(housebill="H1", proNumber="771043317403", vendorId="SAIA")]
    settings = AppSettings(logs_dir=str(tmp_path / "logs"), debug_dir=str(tmp_path / "debug"),
                           dry_run=True, breaker_state_file=str(tmp_path / "breakers.json"))
    spec = saia_spec(breaker_threshold)
    backend = FakeBackend(config, shipments)
    queue = FakeQueue()
    sv = FakeSv(settings, backend, FakeProvider(session), queue)
    summary = asyncio.run(Runner(sv, FakeResolver(spec)).run_once(force=True))
    return summary, queue


def test_breaker_opens_after_threshold_and_persists(tmp_path):
    entered = "77104331740"
    responses = {entered: [CapturedResponse(
        "https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, saia_payload(entered))]}
    label = "CALIX NETWORKS/SAIA"

    # Run 1: scrape runs, hits the challenge, breaker counts 1 block.
    summary, queue = run_once(tmp_path, FakeSession(responses, blocked=True))
    assert summary[label].get("skipped_breaker_open") != 1
    assert queue.alerts, "ChallengeError must raise the intervention alert"
    # Run 2: still under the threshold, scrapes again, breaker opens.
    summary2, queue2 = run_once(tmp_path, FakeSession(responses, blocked=True))
    assert summary2[label].get("skipped_breaker_open") != 1
    # Run 3: breaker open -> group skipped entirely.
    summary3, _ = run_once(tmp_path, FakeSession(responses, blocked=True))
    assert summary3[label]["skipped_breaker_open"] == 1
    assert summary3[label]["requested"] == 1

    # A fresh process (new Runner reading the same state file) stays skipped.
    summary4, _ = run_once(tmp_path, FakeSession(responses, blocked=True))
    assert summary4[label]["skipped_breaker_open"] == 1

    state_file = tmp_path / "breakers.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "CALIX NETWORKS|SAIA|134034" in state
    assert state["CALIX NETWORKS|SAIA|134034"]["failures"] >= 2


def test_successful_run_resets_breaker_and_carries_engine_fields(tmp_path):
    entered = "77104331740"
    responses = {entered: [CapturedResponse(
        "https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, saia_payload(entered))]}
    label = "CALIX NETWORKS/SAIA"

    blocked = FakeSession(responses, blocked=True)
    summary, _ = run_once(tmp_path, blocked)

    # A healthy run records success: the breaker state section is dropped.
    session = FakeSession(responses)
    session.engine_name = "scrapling"
    session.state_reused = True
    summary_ok, _ = run_once(tmp_path, session)
    assert summary_ok[label]["found"] == 1
    assert summary_ok[label]["engine_requested"] == "playwright_stealth"
    assert summary_ok[label]["engine_used"] == "scrapling"
    assert summary_ok[label]["state_reused"] is True
    assert summary_ok[label]["challenge_outcome"] == "clean"
    assert summary_ok[label]["started_at"]

    state = json.loads((tmp_path / "breakers.json").read_text(encoding="utf-8"))
    assert "CALIX NETWORKS|SAIA|134034" not in state, "success must reset the breaker"