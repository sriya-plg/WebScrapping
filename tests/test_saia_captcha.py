"""Tests for Saia CAPTCHA detection and human-assisted verification with Playwright."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.browser.base import BrowserProvider, BrowserSession, CapturedResponse
from app.browser.factory import make_provider
from app.core.scraper import BatchScraper, ChallengeError
from app.models import CarrierConfig, ShipmentRef, TrackingJob
from app.settings import BrowserConfig, CarrierSpec, load_resolver


class CaptchaMockSession(BrowserSession):
    def __init__(
        self,
        bframe_visible: bool = False,
        solve_after_ticks: int | None = None,
        responses: dict | None = None,
    ):
        self.bframe_visible = bframe_visible
        self.solve_after_ticks = solve_after_ticks
        self.responses = responses or {}
        self.ticks = 0
        self.verified = False
        self.state_saved = False
        self.input_value = ""
        self.urls = []
        self.closed = False

    async def goto(self, url):
        self.urls.append(url)

    async def fill(self, selector, value, human=True, frame=None):
        self.input_value = value

    async def set_input_value(self, selector, value, frame=None):
        self.input_value = value

    async def type_text(self, selector, text, delay_ms=0, clear=False, frame=None):
        self.input_value = text

    async def get_value(self, selector, frame=None):
        return self.input_value

    async def click(self, selector, frame=None, force=False):
        pass

    async def press(self, selector, key, frame=None):
        pass

    async def select_option(self, selector, value, frame=None):
        pass

    async def wait_for_selector(self, selector, timeout_s=20, frame=None):
        return True

    async def is_visible(self, selector, frame=None):
        if selector == "#recaptcha-anchor":
            return True
        if selector == "#recaptcha-anchor[aria-checked='true']":
            return self.verified
        return False

    async def evaluate(self, expression, arg=None):
        self.ticks += 1
        if self.solve_after_ticks is not None and self.ticks >= self.solve_after_ticks:
            self.verified = True
            if "g-recaptcha-response" in expression:
                return "mock_captcha_token_12345"
        if "recaptcha/api2/bframe" in expression:
            return self.bframe_visible and not self.verified
        if "g-recaptcha-response" in expression:
            return "mock_captcha_token_12345" if self.verified else ""
        return None

    async def discover_search_box(self):
        return None

    async def capture_all(self, action, timeout_s=30, settle_s=1, until=None):
        await action()
        return self.responses.get(self.input_value, [])

    async def looks_blocked(self, markers=None):
        return False

    async def save_debug(self, path):
        return path

    def current_url(self):
        return "https://www.saia.com/track"

    async def save_state(self):
        self.state_saved = True

    async def close(self):
        self.closed = True


class MockProvider(BrowserProvider):
    def __init__(self, session):
        self.session = session

    async def open_session(self, key):
        return self.session

    async def close(self):
        pass


def make_saia_spec(headless=True, assist=False):
    return CarrierSpec(
        code="SAIA",
        client="calix",
        browser=BrowserConfig(browser_mode="patchright", headless=headless),
        site={
            "tracking_url": "https://www.saia.com/track",
            "input_selector": "#trackingNumbers",
            "submit_selector": "#trackBtn",
            "batch_size": 2,
            "response_url_pattern": "find-by-pro-number",
            "expected_response_shape": "single_batch",
            "response_pro_path": "[].proNumber",
            "action_min_delay_s": 0,
            "action_max_delay_s": 0,
            "idle_window_s": 0.01,
            "extra": {
                "pro_input_digits": 11,
                "interactive_challenge_assist": assist,
                "challenge_assist_wait_s": 2.0,
                "retry_missing_individually": False,
                "single_batch_unmatched_is_not_found": True,
            },
        },
    )


def make_jobs(pros):
    config = CarrierConfig(
        id=1,
        carrierName="Saia LTL Freight",
        carrierCode="SAIA",
        trackingUrl="https://www.saia.com/track",
        billTo=100,
        customerName="Calix",
        isActive=True,
    )
    return [
        TrackingJob(
            carrier=config,
            shipment=ShipmentRef(housebill=f"H{i}", proNumber=p, vendorId="SAIA"),
            search_by="proNumber",
            search_value=p,
        )
        for i, p in enumerate(pros)
    ]


def test_browser_factory_uses_playwright_family_only():
    cfg = BrowserConfig(browser_mode="patchright", headless=True)
    provider = make_provider(cfg, "app/runtime/browser")
    assert provider.__class__.__name__ == "PlaywrightProvider"
    assert provider.engine == "patchright"


def test_captcha_detection_headless_aborts_without_not_found(tmp_path):
    # Headless mode: bframe appears -> ChallengeError is raised
    session = CaptchaMockSession(bframe_visible=True, solve_after_ticks=None)
    session.page = None
    spec = make_saia_spec(headless=True, assist=False)
    jobs = make_jobs(["771375800603", "771375709105"])

    result = asyncio.run(
        BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(jobs, spec)
    )

    # Must NOT classify as not_found!
    assert result.outcomes["771375800603"] == "error"
    assert result.outcomes["771375709105"] == "error"
    assert "not_found" not in result.outcomes.values()

    # Must clearly indicate ChallengeError at submit / recaptcha challenge step
    assert result.error_type == "ChallengeError"
    assert result.failed_step in ("click submit", "recaptcha_challenge")
    assert any("recaptcha" in err["message"].lower() for err in result.errors)


def test_human_assisted_verification_and_resumption(tmp_path):
    # Headed assist mode: bframe appears, then gets solved after 2 ticks
    pro = "771375800603"
    entered = "77137580060"
    payload = [{"proNumber": "77137580060", "status": "Delivered"}]
    responses = {entered: [CapturedResponse("https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, payload)]}

    session = CaptchaMockSession(
        bframe_visible=True,
        solve_after_ticks=2,
        responses=responses,
    )
    session.page = None
    spec = make_saia_spec(headless=False, assist=True)
    jobs = make_jobs([pro])

    result = asyncio.run(
        BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(jobs, spec)
    )

    # Scrape resumes and captures the response successfully
    assert result.missing == []
    assert pro in result.received
    assert result.outcomes[pro] in ("found", "captured")
    # Session state was saved on resolution
    assert session.state_saved is True


def test_human_assisted_verification_timeout(tmp_path):
    # Assist mode enabled, but user never solves it -> times out cleanly
    session = CaptchaMockSession(bframe_visible=True, solve_after_ticks=999)
    session.page = None
    spec = make_saia_spec(headless=False, assist=True)
    spec.site.extra["challenge_assist_wait_s"] = 1.0  # fast test timeout
    jobs = make_jobs(["771375800603"])

    result = asyncio.run(
        BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(jobs, spec)
    )

    assert result.error_type == "ChallengeError"
    assert "timed out waiting for human intervention" in result.error_message
    assert result.outcomes["771375800603"] == "error"


def test_regression_other_carriers_unaffected():
    resolver = load_resolver("app/clients")
    # Estes
    estes_spec = resolver.resolve("100", "ESTES")
    if estes_spec:
        assert estes_spec.browser.browser_mode in (None, "playwright_stealth")
        assert estes_spec.site.input_selector is not None
    # Forward Air
    fwda_spec = resolver.resolve("100", "FORWARDAIR")
    if fwda_spec:
        assert fwda_spec.browser.browser_mode == "patchright"


