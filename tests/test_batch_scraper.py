import asyncio
import json
from pathlib import Path

from app.browser.base import BrowserProvider, BrowserSession, CapturedResponse
from app.core.scraper import BatchScraper, classify_carrier_raw
from app.models import CarrierConfig, ShipmentRef, TrackingJob
from app.settings import CarrierSpec

FIXTURE = Path(__file__).parent / "fixtures" / "estes_response.json"


class MockSession(BrowserSession):
    def __init__(self, responses_by_input, failures=None, visible_selectors=None, eval_results=None):
        self.responses_by_input = responses_by_input
        self.failures = set(failures or [])
        self.visible_selectors = set(visible_selectors or [])
        self.eval_results = eval_results or {}
        self.input_value = ""
        self.inputs = []
        self.filled_values = {}
        self.typed_chunks = []
        self.presses = []
        self.clicks = 0
        self.listener = False
        self.urls = []
        self.closed = False
        self.state_saved = False

    async def goto(self, url): self.urls.append(url)
    async def fill(self, selector, value, human=True, frame=None):
        self.input_value = value
        self.filled_values[selector] = value
    async def set_input_value(self, selector, value, frame=None):
        self.input_value = value
        self.filled_values[selector] = value
    async def type_text(self, selector, text, delay_ms=0, clear=False, frame=None):
        if clear:
            self.input_value = text
        else:
            self.input_value += text
        self.typed_chunks.append((text, delay_ms, clear))
    async def get_value(self, selector, frame=None): return self.input_value
    async def click(self, selector, frame=None, force=False):
        self.clicks += 1
        if self.listener:
            self.inputs.append(self.input_value)
            if self.input_value in self.failures:
                raise RuntimeError("mock click failed")
    async def press(self, selector, key, frame=None):
        self.presses.append(key)
        if key in ("Enter", "Return") and self.input_value and not self.input_value.endswith("\n"):
            self.input_value += "\n"
        elif key in ("Enter", "Return"):
            self.input_value += "\n"
    async def select_option(self, selector, value, frame=None): pass
    async def wait_for_selector(self, selector, timeout_s=20, frame=None): return True
    async def is_visible(self, selector, frame=None): return selector in self.visible_selectors
    async def evaluate(self, expression, arg=None):
        for pattern, res in self.eval_results.items():
            if pattern in expression:
                return res
        return None
    async def discover_search_box(self): return None
    async def capture_all(self, action, timeout_s=30, settle_s=1, until=None):
        self.listener = True
        try:
            await action()
            return self.responses_by_input.get(self.input_value, [])
        finally:
            self.listener = False
    async def looks_blocked(self, markers=None): return False
    async def save_debug(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"mock screenshot")
        return path
    def current_url(self): return self.urls[-1] if self.urls else "https://carrier.test/track?token=mock"
    async def save_state(self): self.state_saved = True
    async def close(self): self.closed = True


class MockProvider(BrowserProvider):
    def __init__(self, session): self.session = session
    async def open_session(self, key): return self.session
    async def close(self): pass


def make_spec(shape="single_batch", batch_size=2):
    return CarrierSpec(code="TEST", client="sample", site={
        "tracking_url": "https://carrier.test/track", "input_selector": "#pro",
        "submit_selector": "#search", "batch_size": batch_size,
        "response_url_pattern": "track-api", "expected_response_shape": shape,
        "response_pro_path": "data[].proNumber", "action_min_delay_s": 0,
        "action_max_delay_s": 0, "idle_window_s": 0.01,
    })


def make_jobs(values):
    config = CarrierConfig(id=1, carrierName="Test Carrier", carrierCode="TEST",
        trackingUrl="https://carrier.test/track", billTo=100, customerName="Sample",
        isActive=True, userName=None, password=None)
    return [TrackingJob(carrier=config,
        shipment=ShipmentRef(housebill=f"H{i}", proNumber=value, vendorId="TEST"),
        search_by="proNumber", search_value=value) for i, value in enumerate(values)]


def response(data): return CapturedResponse("https://carrier.test/track-api", 200, data)


def by_input(values):
    return {"data": [{"proNumber": v, "status": f"raw-{v}"} for v in values.split("\n")]}


def test_chunk_boundaries_and_single_submit_per_chunk(tmp_path):
    async def check(values, expected_clicks):
        inputs = [values[i:i + 2] for i in range(0, len(values), 2)]
        responses = {"\n".join(chunk): [response(by_input("\n".join(chunk)))] for chunk in inputs}
        session = MockSession(responses)
        result = await BatchScraper(MockProvider(session), make_spec(batch_size=2), str(tmp_path)).run_batch(
            make_jobs(values), make_spec(batch_size=2))
        assert session.clicks == expected_clicks
        assert result.missing == []
        assert len(result.received) == len(values)
    asyncio.run(check(["1", "2"], 1))
    asyncio.run(check(["1", "2", "3"], 2))


def test_per_pro_collects_each_response_and_uses_saved_fixture(tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    fixture["proNumber"] = "001-23"
    second = {"proNumber": "4", "raw": "second"}
    session = MockSession({"000123\n4": [response(fixture), response(second)]})
    spec = make_spec("per_pro")
    spec.site.response_pro_path = "proNumber"
    spec.site.strip_leading_zeros = True
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["000123", "4"]), spec))
    assert result.missing == [], (result.errors, session.inputs, spec.site.response_url_pattern)
    assert result.received["000123"] == fixture
    assert result.received["4"] == second


def test_classify_estes_not_found_payload():
    spec = make_spec("per_pro", batch_size=1)
    spec.site.extra = {
        "response_data_path": "data",
        "response_error_code_path": "error.code",
        "response_error_message_path": "error.message",
        "response_not_found_codes": [50010],
    }
    raw = {"data": [], "error": {"code": 50010, "message": "Not found.", "details": ""}}
    outcome, summary = classify_carrier_raw(raw, spec)
    assert outcome == "not_found"
    assert summary["error_message"] == "Not found."
    assert summary["data_count"] == 0


def test_estes_not_found_json_still_received(tmp_path):
    url = "https://myestes-api.estes-express.com/shipmenttracking/history?pro=1848508033"
    body = {"data": [], "error": {"code": 50010, "message": "Not found.", "details": ""}}
    session = MockSession({"1848508033": [CapturedResponse(url, 200, body)]})
    spec = make_spec("per_pro", batch_size=1)
    spec.site.response_url_pattern = "myestes-api"
    spec.site.response_pro_path = "data[].pro"
    spec.site.strip_spaces_dashes = True
    spec.site.extra = {
        "retry_missing_individually": False,
        "response_data_path": "data",
        "response_error_code_path": "error.code",
        "response_error_message_path": "error.message",
        "response_not_found_codes": [50010],
    }
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["184-8508033"]), spec))
    assert result.missing == []
    assert result.outcomes["184-8508033"] == "not_found"
    assert result.received["184-8508033"] == body


def test_url_query_pro_maps_response_without_json_path(tmp_path):
    url = "https://myestes-api.estes-express.com/shipmenttracking/history?pro=1848508033"
    body = {"status": "In Transit", "reference": "1848508033"}
    session = MockSession({"1848508033": [CapturedResponse(url, 200, body)]})
    spec = make_spec("per_pro", batch_size=1)
    spec.site.response_url_pattern = "myestes-api"
    spec.site.response_pro_path = "data[].pro"
    spec.site.strip_spaces_dashes = True
    spec.site.extra = {"retry_missing_individually": False}
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["184-8508033"]), spec))
    assert result.missing == []
    assert "184-8508033" in result.received
    assert result.received["184-8508033"] == body
    assert result.scrape_items()[0]["search_value"] == "184-8508033"


def test_retry_missing_disabled_leaves_pros_in_missing(tmp_path):
    session = MockSession({"1\n2": [response(by_input("1"))]})
    spec = make_spec("single_batch")
    spec.site.extra = {"retry_missing_individually": False}
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["1", "2"]), spec))
    assert "1" in result.received
    assert result.missing == ["2"]
    assert session.inputs == ["1\n2"]


def test_missing_pro_retried_individually(tmp_path):
    session = MockSession({"1\n2": [response(by_input("1"))],
                           "2": [response(by_input("2"))]})
    spec = make_spec("single_batch")
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["1", "2"]), spec))
    assert result.missing == []
    assert session.inputs == ["1\n2", "2"]


def test_failed_chunk_falls_back_and_later_chunk_continues(tmp_path):
    session = MockSession({"1": [], "2": [response(by_input("2"))]}, failures={"1"})
    spec = make_spec(batch_size=1)
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["1", "2"]), spec))
    assert "2" in result.received
    assert result.missing == ["1"]
    assert result.errors
    assert session.inputs == ["1", "1", "2"]


def test_unfilled_carrier_todos_fail_fast_without_browser_actions(tmp_path):
    session = MockSession({})
    spec = make_spec()
    spec.site.submit_selector = "TODO_REPLACE_SEARCH_BUTTON"
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["123"]), spec))
    assert result.missing == ["123"]
    assert result.errors[0]["type"] == "ScrapeConfigError"
    assert session.clicks == 0 and session.urls == []


def test_login_disabled_default_keeps_public_tracking(tmp_path):
    session = MockSession({"123": [response(by_input("123"))]})
    spec = make_spec(batch_size=1)
    spec.site.login = {
        "enabled": False,
        "url": "https://carrier.test/login",
        "selectors": {"username": "#user", "password": "#pwd", "submit": "#btn"},
    }
    jobs = make_jobs(["123"])
    jobs[0].carrier.user_name = "test_user"
    from pydantic import SecretStr
    jobs[0].carrier.password = SecretStr("test_pass")

    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(jobs, spec))
    assert result.missing == []
    assert len(result.received) == 1
    # Login URL was never visited
    assert "https://carrier.test/login" not in session.urls
    assert "#user" not in session.filled_values


def test_login_enabled_success_flow(tmp_path):
    session = MockSession(
        {"123": [response(by_input("123"))]},
        visible_selectors={"#account-menu"},
    )
    spec = make_spec(batch_size=1)
    spec.site.login = {
        "enabled": True,
        "url": "https://carrier.test/login",
        "selectors": {
            "username": "#user",
            "password": "#pwd",
            "submit": "#login-btn",
            "success": "#account-menu",
        },
    }
    jobs = make_jobs(["123"])
    jobs[0].carrier.user_name = "portal_user"
    from pydantic import SecretStr
    jobs[0].carrier.password = SecretStr("portal_secret")

    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(jobs, spec))
    assert result.missing == []
    assert len(result.received) == 1
    assert "https://carrier.test/login" in session.urls
    assert session.filled_values.get("#user") == "portal_user"
    assert session.filled_values.get("#pwd") == "portal_secret"
    assert session.state_saved is True


def test_login_enabled_missing_credentials_fails_cleanly(tmp_path):
    session = MockSession({})
    spec = make_spec(batch_size=1)
    spec.site.login = {
        "enabled": True,
        "required": True,
        "url": "https://carrier.test/login",
        "selectors": {"username": "#user", "password": "#pwd", "submit": "#btn"},
    }
    jobs = make_jobs(["123"])
    jobs[0].carrier.user_name = None
    jobs[0].carrier.password = None

    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(jobs, spec))
    assert result.missing == ["123"]
    assert any("LoginCredentialsMissingError" in err.get("type", "") for err in result.errors)


def test_login_failure_detected_from_dom(tmp_path):
    session = MockSession(
        {},
        eval_results={"invalid credentials": True},
    )
    spec = make_spec(batch_size=1)
    spec.site.login = {
        "enabled": True,
        "required": True,
        "url": "https://carrier.test/login",
        "selectors": {"username": "#user", "password": "#pwd", "submit": "#btn", "success": "#account"},
    }
    jobs = make_jobs(["123"])
    jobs[0].carrier.user_name = "wrong_user"
    from pydantic import SecretStr
    jobs[0].carrier.password = SecretStr("wrong_pass")

    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(jobs, spec))
    assert result.missing == ["123"]
    assert any("LoginFailedError" in err.get("type", "") for err in result.errors)


def test_login_mfa_challenge_detected_from_dom(tmp_path):
    session = MockSession(
        {},
        eval_results={"two-factor": True},
    )
    spec = make_spec(batch_size=1)
    spec.site.login = {
        "enabled": True,
        "required": True,
        "url": "https://carrier.test/login",
        "selectors": {"username": "#user", "password": "#pwd", "submit": "#btn", "success": "#account"},
    }
    jobs = make_jobs(["123"])
    jobs[0].carrier.user_name = "mfa_user"
    from pydantic import SecretStr
    jobs[0].carrier.password = SecretStr("mfa_pass")

    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(jobs, spec))
    assert result.missing == ["123"]
    assert any("MFAChallengeError" in err.get("type", "") for err in result.errors)


def test_saia_root_array_maps_each_pro(tmp_path):
    pros = ["771043317403", "771045181604"]
    payload = [
        {"referenceNumbers": {"proNumber": "771043317403"}, "shipment": {"status": "In Transit"}},
        {"referenceNumbers": {"proNumber": "771045181604"}, "shipment": {"status": "Delivered"}},
    ]
    entered = "\n".join(["77104331740", "77104518160"])
    session = MockSession(
        {entered: [CapturedResponse("https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, payload)]}
    )
    spec = make_spec("single_batch", batch_size=2)
    spec.site.response_url_pattern = "find-by-pro-number"
    spec.site.response_pro_path = "[].referenceNumbers.proNumber"
    spec.site.extra = {"pro_input_digits": 11, "retry_missing_individually": False}
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(pros), spec))
    assert result.missing == []
    assert len(result.received) == 2
    assert result.received["771043317403"]["shipment"]["status"] == "In Transit"
    assert result.outcomes["771043317403"] == "found"


def test_preferred_full_details_overwrites_summary_without_wiping_siblings(tmp_path):
    summary = [
        {"referenceNumbers": {"proNumber": "771043317403"}, "shipment": {"status": "In Transit"}},
        {"referenceNumbers": {"proNumber": "771045181604"}, "shipment": {"status": "In Transit"}},
    ]
    details = {
        "referenceNumbers": {"proNumber": "771043317403"},
        "shipment": {"status": "Delivered"},
        "history": [{"date": "2026-10-11T14:30:00", "description": "Delivered", "status": "Delivered"}],
    }
    entered = "\n".join(["77104331740", "77104518160"])
    session = MockSession({
        entered: [
            CapturedResponse("https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, summary),
            CapturedResponse("https://www.saia.com/api/v1/tracking/find-full-details", 200, details),
        ]
    })
    spec = make_spec("single_batch", batch_size=2)
    spec.site.response_url_pattern = "(?i)find-full-details?|find-by-pro-number"
    spec.site.response_url_prefer = "find-full-detail"
    spec.site.response_pro_path = "[].referenceNumbers.proNumber"
    spec.site.extra = {
        "pro_input_digits": 11,
        "retry_missing_individually": False,
        "single_batch_unmatched_is_not_found": True,
    }
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["771043317403", "771045181604"]), spec))
    assert result.missing == []
    assert result.received["771043317403"]["shipment"]["status"] == "Delivered"
    assert result.received["771043317403"]["history"]
    assert result.received["771045181604"]["shipment"]["status"] == "In Transit"
    assert result.outcomes["771045181604"] == "found"


def test_saia_empty_array_records_not_found_per_pro(tmp_path):
    session = MockSession(
        {"77104331740": [CapturedResponse("https://www.saia.com/api/v1/tracking/find-by-pro-number", 200, [])]}
    )
    spec = make_spec("single_batch", batch_size=1)
    spec.site.response_url_pattern = "find-by-pro-number"
    spec.site.response_pro_path = "[].referenceNumbers.proNumber"
    spec.site.extra = {"pro_input_digits": 11, "retry_missing_individually": False}
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["771043317403"]), spec))
    assert result.missing == []
    assert result.received["771043317403"] == []
    assert result.outcomes["771043317403"] == "not_found"


def test_multiline_saia_bulk_entry_and_eleven_digit_pros(tmp_path):
    pro_list = ["771043317403", "771045181604", "771045854802"]
    expected_multiline = "\n".join(["77104331740", "77104518160", "77104585480"])

    session = MockSession({expected_multiline: [response(by_input(expected_multiline))]})
    spec = make_spec("single_batch", batch_size=3)
    spec.site.input_mode = "multiline"
    spec.site.delimiter = "\n"
    spec.site.extra = {"pro_input_digits": 11}
    spec.site.pre_submit_delay_s = 0.05

    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(pro_list), spec))

    assert result.missing == []
    assert len(result.received) == 3
    assert session.typed_chunks == []
    assert session.presses == []
    assert session.input_value == expected_multiline


def test_instant_entry_estes_preserves_behavior_no_regression(tmp_path):
    pro_list = ["11111", "22222"]
    expected_input = "11111\n22222"
    session = MockSession({expected_input: [response(by_input(expected_input))]})
    spec = make_spec("single_batch", batch_size=2)
    spec.site.typing_delay_ms = 0
    spec.site.inter_pro_delay_s = 0

    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(pro_list), spec))

    assert result.missing == []
    # No gradual typing chunks; filled directly
    assert session.typed_chunks == []
    assert session.input_value == expected_input


def test_recaptcha_image_challenge_blocks_submission(tmp_path):
    # Simulate recaptcha present and image challenge visible in bframe
    session = MockSession(
        {},
        visible_selectors={"#recaptcha-anchor"},
        eval_results={"recaptcha/api2/bframe": True},
    )
    session.page = None  # force fallback to evaluate
    spec = make_spec("single_batch", batch_size=1)

    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["999"]), spec))

    assert result.missing == ["999"]
    # Verify that ChallengeError was recorded and not submitted as success
    assert any("ChallengeError" in err.get("type", "") for err in result.errors)


def test_gradual_human_typing_enters_chunks_with_delays(tmp_path):
    pro_list = ["11111", "22222"]
    expected_input = "11111\n22222"
    session = MockSession({expected_input: [response(by_input(expected_input))]})
    spec = make_spec("single_batch", batch_size=2)
    spec.site.typing_delay_ms = 75
    spec.site.inter_pro_delay_s = 0.5

    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(pro_list), spec))

    assert result.missing == []
    assert len(session.typed_chunks) >= 3  # initial clear, pro 1, separator, pro 2
    assert session.input_value == expected_input
    assert any(chunk[1] == 75 for chunk in session.typed_chunks)


def test_single_batch_unmatched_is_not_found_records_not_found(tmp_path):
    session = MockSession({"1\n2": [response(by_input("1"))]})
    spec = make_spec("single_batch", batch_size=2)
    spec.site.extra = {
        "retry_missing_individually": False,
        "single_batch_unmatched_is_not_found": True,
    }
    result = asyncio.run(BatchScraper(MockProvider(session), spec, str(tmp_path)).run_batch(
        make_jobs(["1", "2"]), spec))
    assert result.missing == []
    assert "1" in result.received
    assert "2" in result.received
    assert result.outcomes["1"] in ("found", "captured")
    assert result.outcomes["2"] == "not_found"


def test_get_path_handles_unpacked_dict_with_index_zero():
    from app.core.mapper import get_path
    raw_dict = {"referenceNumbers": {"proNumber": "771043317403"}, "shipment": {"status": "In Transit"}}
    raw_list = [{"referenceNumbers": {"proNumber": "771043317403"}, "shipment": {"status": "In Transit"}}]

    assert get_path(raw_dict, "[0].referenceNumbers.proNumber") == "771043317403"
    assert get_path(raw_dict, "referenceNumbers.proNumber") == "771043317403"
    assert get_path(raw_list, "[0].referenceNumbers.proNumber") == "771043317403"
    assert get_path(raw_dict, "[0].shipment.status") == "In Transit"


def test_setup_failure_records_error_with_pros(tmp_path):
    class CrashingProvider(BrowserProvider):
        async def open_session(self, key: str) -> BrowserSession:
            raise RuntimeError("Browser launch failed: DISPLAY missing")
        async def close(self): pass

    spec = make_spec("per_pro", batch_size=2)
    jobs = make_jobs(["PRO1", "PRO2"])
    result = asyncio.run(BatchScraper(CrashingProvider(), spec, str(tmp_path)).run_batch(jobs, spec))

    assert len(result.errors) == 1
    assert result.errors[0]["type"] == "RuntimeError"
    assert "PRO1,PRO2" in result.errors[0]["pros"]
    assert result.missing == ["PRO1", "PRO2"]


