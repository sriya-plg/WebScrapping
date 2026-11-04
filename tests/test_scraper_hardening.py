"""Scraper hardening: TimeoutError chunk retry, CF wait-and-poll, PRO collision, outcome fields."""
import asyncio
import logging

from app.browser.base import BrowserProvider, BrowserSession, CapturedResponse
from app.core.scraper import BatchScraper, CHALLENGE_RANK
from app.models import CarrierConfig, ShipmentRef, TrackingJob
from app.settings import CarrierSpec


class FakeSession(BrowserSession):
    """Session scriptable per test: block sequence, capture failures, engine metadata."""

    def __init__(self, responses_by_input=None, block_sequence=None, capture_error=None,
                 engine_name=None, state_reused=None, goto_status=200):
        self.responses_by_input = responses_by_input or {}
        self.blocked = list(block_sequence or [])
        self.capture_error = list(capture_error or [])
        self.capture_calls = 0
        self.goto_calls = 0
        self.input_value = ""
        self.urls = []
        self.clicks = 0
        self.closed = False
        self.state_saved = False
        self.engine_name = engine_name
        self.state_reused = state_reused
        self.goto_status = goto_status

    async def goto(self, url):
        self.goto_calls += 1
        self.urls.append(url)
        return type("Resp", (), {"status": self.goto_status})()

    async def fill(self, selector, value, human=True, frame=None): self.input_value = value
    async def set_input_value(self, selector, value, frame=None): self.input_value = value
    async def type_text(self, selector, text, delay_ms=0, clear=False, frame=None):
        self.input_value = (text if clear else self.input_value + text)
    async def get_value(self, selector, frame=None): return self.input_value
    async def click(self, selector, frame=None, force=False):
        self.clicks += 1
    async def press(self, selector, key, frame=None): pass
    async def select_option(self, selector, value, frame=None): pass
    async def wait_for_selector(self, selector, timeout_s=20, frame=None): return True
    async def is_visible(self, selector, frame=None): return False
    async def evaluate(self, expression, arg=None): return None
    async def discover_search_box(self): return None
    async def capture_all(self, action, timeout_s=30, settle_s=1, until=None):
        self.capture_calls += 1
        if self.capture_error:
            raise self.capture_error.pop(0)
        await action()
        return self.responses_by_input.get(self.input_value, [])
    async def looks_blocked(self, markers=None):
        if self.blocked:
            return self.blocked.pop(0)
        return False
    async def save_debug(self, path): return path
    def current_url(self): return self.urls[-1] if self.urls else "https://www.saia.com/track"
    async def save_state(self): self.state_saved = True
    async def close(self): self.closed = True


class RecordingProvider(BrowserProvider):
    def __init__(self, session):
        self.session, self.opened = session, []

    async def open_session(self, key):
        self.opened.append(key)
        return self.session

    async def close(self): pass


def make_spec(limits=None, **site_overrides):
    site = {
        "tracking_url": "https://www.saia.com/track", "input_selector": "#trackingNumbers",
        "submit_selector": "#trackBtn", "batch_size": 2,
        "response_url_pattern": "find-by-pro-number", "expected_response_shape": "single_batch",
        "response_pro_path": "[].proNumber", "action_min_delay_s": 0, "action_max_delay_s": 0,
        "idle_window_s": 0.01, "challenge_wait_s": 0.05,
        "extra": {"pro_input_digits": 11, "retry_missing_individually": False},
    }
    site.update(site_overrides)
    return CarrierSpec(code="SAIA", client="calix",
                        limits=limits or {"retries": 0, "breaker_threshold": 99,
                                           "breaker_cooldown_s": 0},
                        block_markers=["just a moment"], site=site)


def make_jobs(values):
    config = CarrierConfig(id=1, carrierName="Saia", carrierCode="SAIA",
                            trackingUrl="https://www.saia.com/track", billTo=134034,
                            customerName="CALIX NETWORKS", isActive=True)
    return [TrackingJob(carrier=config,
                        shipment=ShipmentRef(housebill=f"H{i}", proNumber=value, vendorId="SAIA"),
                        search_by="proNumber", search_value=value) for i, value in enumerate(values)]


def saia_payload(pros):
    return [{"referenceNumbers": {"proNumber": pro}, "shipment": {"status": "In Transit"}}
            for pro in pros]


def run(provider, spec, jobs, tmp_path):
    return asyncio.run(BatchScraper(provider, spec, str(tmp_path / "debug")).run_batch(jobs, spec))


def test_timeout_error_retries_the_chunk(tmp_path, caplog):
    entered = "77104331740"
    responses = {entered: [CapturedResponse(
        "https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, saia_payload([entered]))]}
    session = FakeSession(responses, capture_error=[TimeoutError("no tracking JSON matched")])
    spec = make_spec(limits={"retries": 1, "breaker_threshold": 99, "breaker_cooldown_s": 0})
    provider = RecordingProvider(session)

    result = run(provider, spec, make_jobs(['771043317403']), tmp_path)

    assert session.capture_calls == 2, "one TimeoutError should be retried once"
    assert result.errors == []
    assert result.outcomes["771043317403"] == "found"
    assert any("retrying chunk" in rec.message for rec in caplog.records)


def test_non_timeout_errors_are_not_retried(tmp_path):
    session = FakeSession({}, capture_error=[RuntimeError("carrier exploded")])
    spec = make_spec(limits={"retries": 2, "breaker_threshold": 99, "breaker_cooldown_s": 0})
    provider = RecordingProvider(session)

    result = run(provider, spec, make_jobs(['771043317403']), tmp_path)

    assert session.capture_calls == 1, "only TimeoutError is retried"
    assert result.errors and result.errors[0]["type"] == "RuntimeError"
    assert result.outcomes["771043317403"] == "error"


def test_blocked_then_clearing_does_not_fail(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    entered = "77104331740"
    responses = {entered: [CapturedResponse(
        "https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, saia_payload([entered]))]}
    # First looks_blocked() call (after goto) -> blocked; the wait polls -> cleared.
    session = FakeSession(responses, block_sequence=[True, False])
    spec = make_spec(challenge_wait_s=1.0)

    result = run(RecordingProvider(session), spec, make_jobs(['771043317403']), tmp_path)

    assert result.errors == []
    assert result.outcomes["771043317403"] == "found"
    assert any("waiting up to" in rec.message for rec in caplog.records)


def test_blocked_never_clearing_raises_after_the_wait(tmp_path):
    session = FakeSession({}, block_sequence=[True] * 8)
    spec = make_spec(challenge_wait_s=0.1)

    result = run(RecordingProvider(session), spec, make_jobs(['771043317403']), tmp_path)

    assert result.error_type == "ChallengeError"
    assert result.outcomes["771043317403"] == "error"
    assert result.challenge_outcome == "cloudflare_block"


def test_status_code_block_reloads_once_then_raises(tmp_path):
    # Text markers clear but every navigation still returns 403 -> same wait-then-raise path.
    session = FakeSession({}, goto_status=403)
    spec = make_spec(challenge_wait_s=0.1)

    result = run(RecordingProvider(session), spec, make_jobs(['771043317403']), tmp_path)

    assert session.goto_calls == 2, "status block reloads once to re-check"
    assert result.error_type == "ChallengeError"
    assert result.challenge_outcome == "cloudflare_block"


def test_ambiguous_eleven_digit_pros_fail_loudly_and_are_not_submitted(tmp_path):
    entered = "77104331740"
    # Two distinct backend PROs sharing the same 11-digit truncated prefix.
    responses = {entered: [CapturedResponse(
        "https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, saia_payload([entered]))]}
    session = FakeSession(responses)
    spec = make_spec(batch_size=2)
    provider = RecordingProvider(session)

    result = run(provider, spec, make_jobs(["771043317403", "771043317404"]), tmp_path)

    assert provider.opened == [], "ambiguous PROs must never be submitted"
    assert session.clicks == 0
    for pro in ("771043317403", "771043317404"):
        assert result.outcomes[pro] == "error"
        assert result.summaries[pro]["failed_step"] == "pro_normalization"
    assert sorted(result.missing) == ["771043317403", "771043317404"]
    assert len(result.errors) == 2


def test_engine_metadata_flows_into_result(tmp_path):
    entered = "77104331740"
    responses = {entered: [CapturedResponse(
        "https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, saia_payload([entered]))]}
    session = FakeSession(responses, engine_name="scrapling", state_reused=True)
    spec = make_spec()

    result = run(RecordingProvider(session), spec, make_jobs(['771043317403']), tmp_path)

    assert result.engine_requested == "playwright_stealth"  # spec default, no browser_mode set
    assert result.engine_used == "scrapling"
    assert result.state_reused is True
    assert result.challenge_outcome == "clean"
    assert result.started_at


def test_other_error_challenge_outcome(tmp_path):
    session = FakeSession({}, capture_error=[ValueError("input rejected")])
    spec = make_spec()

    result = run(RecordingProvider(session), spec, make_jobs(['771043317403']), tmp_path)

    assert result.challenge_outcome == "other_error"


def test_note_challenge_keeps_most_severe():
    scraper = BatchScraper(RecordingProvider(FakeSession()))
    scraper._note_challenge("cloudflare_block")
    scraper._note_challenge("checkbox_auto_passed")  # less severe: ignored
    assert scraper._challenge_outcome == "cloudflare_block"
    scraper._note_challenge("image_challenge")
    assert scraper._challenge_outcome == "image_challenge"
    assert CHALLENGE_RANK["image_challenge"] > CHALLENGE_RANK["cloudflare_block"]