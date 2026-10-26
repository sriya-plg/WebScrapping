"""Unit tests for diagnostics, traced_step, outcome classification, and error hooks."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from unittest.mock import MagicMock

import pytest

from app.browser.base import BrowserProvider, BrowserSession
from app.core.carrier_activity import post_scrape_activities
from app.core.diagnostics import (
    check_block_page,
    get_environment_report,
    install_exception_hooks,
    mask_proxy_url,
    setup_asyncio_exception_handler,
    traced_step,
)
from app.core.scraper import BatchScraper, ScrapeBatchResult
from app.models import CarrierConfig, ShipmentRef, TrackingJob
from tests.test_batch_scraper import make_spec


def _job(housebill: str, pro: str) -> TrackingJob:
    config = CarrierConfig(
        id=1,
        carrierName="Saia",
        carrierCode="SAI001",
        trackingUrl="https://www.saia.com/track",
        billTo=134034,
        customerName="Calix",
        isActive=True,
        userName="testuser",
        password=None,
    )
    return TrackingJob(
        carrier=config,
        shipment=ShipmentRef(housebill=housebill, proNumber=pro, vendorId="SAI001"),
        search_by="proNumber",
        search_value=pro,
    )


class RecordingBackend:
    def __init__(self):
        self.payloads: list[dict] = []

    async def add_carrier_activity(self, payload):
        self.payloads.append(payload)
        return {"ok": True, "status_code": 200, "body": {}}


# -----------------------------------------------------------------------------
# 1. traced_step Helper (Success, Failure, Re-raise)
# -----------------------------------------------------------------------------

def test_traced_step_sync_success(caplog):
    test_logger = logging.getLogger("test_traced_step_sync")
    with caplog.at_level("INFO", logger="test_traced_step_sync"):
        with traced_step(2, "launch browser", log_obj=test_logger) as step:
            step.set_info(channel="chrome")

    logs = caplog.text
    assert "[STEP 2/13] launch browser START" in logs
    assert "[STEP 2/13] launch browser OK elapsed_ms=" in logs
    assert "channel=chrome" in logs


@pytest.mark.asyncio
async def test_traced_step_async_success(caplog):
    test_logger = logging.getLogger("test_traced_step_async")
    with caplog.at_level("INFO", logger="test_traced_step_async"):
        async with traced_step(7, "navigate to tracking URL", log_obj=test_logger) as step:
            await asyncio.sleep(0.01)
            step.set_info(http_status=200)

    logs = caplog.text
    assert "[STEP 7/13] navigate to tracking URL START" in logs
    assert "[STEP 7/13] navigate to tracking URL OK elapsed_ms=" in logs
    assert "http_status=200" in logs


def test_traced_step_sync_failure_and_reraise(caplog):
    test_logger = logging.getLogger("test_traced_step_fail")
    with caplog.at_level("INFO", logger="test_traced_step_fail"):
        with pytest.raises(NotImplementedError) as exc_info:
            with traced_step(1, "start playwright driver", log_obj=test_logger):
                raise NotImplementedError("SelectorEventLoop cannot spawn subprocess on Windows")

    exc = exc_info.value
    assert getattr(exc, "failed_step") == "start playwright driver"
    assert getattr(exc, "failed_step_num") == 1
    assert getattr(exc, "elapsed_ms") >= 0

    logs = caplog.text
    assert "[STEP 1/13] start playwright driver START" in logs
    assert "[STEP 1/13] start playwright driver FAILED" in logs
    assert "error_type=NotImplementedError" in logs
    assert "SelectorEventLoop cannot spawn subprocess on Windows" in logs


@pytest.mark.asyncio
async def test_traced_step_async_failure_and_reraise(caplog):
    test_logger = logging.getLogger("test_traced_step_async_fail")
    with caplog.at_level("INFO", logger="test_traced_step_async_fail"):
        with pytest.raises(RuntimeError) as exc_info:
            async with traced_step("launch browser", log_obj=test_logger):
                raise RuntimeError("Executable not found")

    exc = exc_info.value
    assert getattr(exc, "failed_step") == "launch browser"
    assert getattr(exc, "failed_step_num") == 2
    logs = caplog.text
    assert "[STEP 2/13] launch browser FAILED" in logs
    assert "error_type=RuntimeError" in logs


# -----------------------------------------------------------------------------
# 2. Error-Outcome Classification
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_outcome_classified_as_error_on_launch_failure(tmp_path):
    class LaunchFailingProvider(BrowserProvider):
        async def open_session(self, key: str) -> BrowserSession:
            exc = RuntimeError("Executable not found at C:\\browsers\\chromium.exe")
            exc.failed_step = "launch browser"
            raise exc

        async def close(self):
            pass

    spec = make_spec("per_pro", batch_size=2)
    jobs = [_job("HB1", "PRO1"), _job("HB2", "PRO2")]
    scraper = BatchScraper(LaunchFailingProvider(), spec, str(tmp_path))

    result = await scraper.run_batch(jobs, spec)

    assert result.outcomes["PRO1"] == "error"
    assert result.outcomes["PRO2"] == "error"
    assert result.failed_step == "launch browser"
    assert result.error_type == "RuntimeError"
    assert "Executable not found" in result.error_message
    assert result.first_error == "RuntimeError@launch browser"
    assert "PRO1" in result.missing
    assert "PRO2" in result.missing


@pytest.mark.asyncio
async def test_outcome_classified_as_missing_when_carrier_returns_nothing(tmp_path):
    from tests.test_batch_scraper import MockProvider, MockSession

    # Scrape succeeds for PRO1, but carrier genuinely returns nothing for PRO2
    responses = {
        "PRO1\nPRO2": [
            CapturedResponse("https://carrier.test/api/track", 200, [{"proNumber": "PRO1", "status": "In Transit"}])
        ],
        "PRO2": [],
    }
    session = MockSession(responses, visible_selectors={"#pro", "#submit"})

    spec = make_spec("single_batch", batch_size=2)
    spec.site.response_url_pattern = "/api/track"
    spec.site.response_pro_path = "[].proNumber"
    spec.site.extra = {"retry_missing_individually": False}

    jobs = [_job("HB1", "PRO1"), _job("HB2", "PRO2")]
    scraper = BatchScraper(MockProvider(session), spec, str(tmp_path))

    result = await scraper.run_batch(jobs, spec)

    assert result.outcomes["PRO1"] == "found"
    assert result.outcomes["PRO2"] == "missing"
    assert result.missing == ["PRO2"]
    assert result.errors == []
    assert result.first_error is None


# -----------------------------------------------------------------------------
# 3. TMS Posting Skip for outcome=error vs genuine missing
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tms_skip_on_error_outcome_default(caplog):
    jobs = [_job("HB1", "PRO1")]
    outcome = ScrapeBatchResult(
        requested=["PRO1"],
        received={},
        outcomes={"PRO1": "error"},
        summaries={"PRO1": {"error_type": "RuntimeError", "error_message": "Browser launch failed", "failed_step": "launch browser"}},
        missing=["PRO1"],
        failed_step="launch browser",
        error_type="RuntimeError",
        error_message="Browser launch failed",
        first_error="RuntimeError@launch browser",
    )
    spec = make_spec("per_pro", batch_size=1)
    backend = RecordingBackend()

    class Settings:
        dry_run = False
        post_errors_to_tms = False

    with caplog.at_level("WARNING"):
        stats = await post_scrape_activities(Settings(), backend, jobs, spec, "run_test", outcome)

    # Should be SKIPPED by default, not posted to TMS
    assert stats["posted"] == 0
    assert stats["skipped"] == 1
    assert len(backend.payloads) == 0
    assert "skipping TMS post, scrape error" in caplog.text


@pytest.mark.asyncio
async def test_tms_post_on_error_outcome_when_enabled():
    jobs = [_job("HB1", "PRO1")]
    outcome = ScrapeBatchResult(
        requested=["PRO1"],
        received={},
        outcomes={"PRO1": "error"},
        summaries={"PRO1": {"error_type": "NotImplementedError", "error_message": "SelectorEventLoop", "failed_step": "start driver"}},
        missing=["PRO1"],
        failed_step="start driver",
        error_type="NotImplementedError",
        error_message="SelectorEventLoop",
        first_error="NotImplementedError@start driver",
    )
    spec = make_spec("per_pro", batch_size=1)
    backend = RecordingBackend()

    class Settings:
        dry_run = False
        post_errors_to_tms = True

    stats = await post_scrape_activities(Settings(), backend, jobs, spec, "run_test", outcome)

    assert stats["posted"] == 1
    assert len(backend.payloads) == 1
    payload = backend.payloads[0]
    assert payload["mlStatus"] == 3
    assert "NotImplementedError: SelectorEventLoop @start driver" in payload["mlError"]
    assert "outcome=error" in payload["mlLog"]


@pytest.mark.asyncio
async def test_tms_post_on_genuine_missing_populates_ml_error():
    jobs = [_job("HB1", "PRO1")]
    outcome = ScrapeBatchResult(
        requested=["PRO1"],
        received={},
        outcomes={"PRO1": "missing"},
        summaries={"PRO1": {}},
        missing=["PRO1"],
    )
    spec = make_spec("per_pro", batch_size=1)
    backend = RecordingBackend()

    class Settings:
        dry_run = False
        post_errors_to_tms = False

    stats = await post_scrape_activities(Settings(), backend, jobs, spec, "run_test", outcome)

    assert stats["posted"] == 1
    assert len(backend.payloads) == 1
    payload = backend.payloads[0]
    assert payload["mlStatus"] == 3
    assert "outcome=missing" in payload["mlLog"]
    assert payload["mlError"] != ""
    assert "missing" in payload["mlError"]


# -----------------------------------------------------------------------------
# 4. Secret Masking in Environment Report
# -----------------------------------------------------------------------------

def test_mask_proxy_url():
    assert mask_proxy_url("http://user:secret123@proxy.example.com:8080") == "http://user:***@proxy.example.com:8080"
    assert mask_proxy_url("http://proxy.example.com:8080") == "http://proxy.example.com:8080"
    assert mask_proxy_url(None) == ""


def test_environment_report_masks_proxies_and_carrier_secrets(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://myuser:mypassword@corporate-proxy.com:3128")
    monkeypatch.setenv("HTTPS_PROXY", "https://admin:supersecret@secure-proxy.com:8443")

    report = get_environment_report()
    assert report["proxies"]["HTTP_PROXY"] == "http://myuser:***@corporate-proxy.com:3128"
    assert report["proxies"]["HTTPS_PROXY"] == "https://admin:***@secure-proxy.com:8443"
    assert "mypassword" not in str(report)
    assert "supersecret" not in str(report)


# -----------------------------------------------------------------------------
# 5. Exception Hooks & Block Page Detection
# -----------------------------------------------------------------------------

def test_exception_hooks(caplog):
    test_logger = logging.getLogger("test_hooks")
    install_exception_hooks(test_logger)

    # Test sys.excepthook
    with caplog.at_level("CRITICAL", logger="test_hooks"):
        try:
            raise ValueError("Test uncaught sys exception")
        except ValueError:
            sys.excepthook(*sys.exc_info())

    assert "Uncaught sys exception: ValueError: Test uncaught sys exception" in caplog.text


def test_asyncio_exception_handler(caplog):
    test_logger = logging.getLogger("test_async_hook")
    loop = asyncio.new_event_loop()
    try:
        setup_asyncio_exception_handler(loop, test_logger)
        with caplog.at_level("ERROR", logger="test_async_hook"):
            exc = RuntimeError("Unhandled loop error")
            loop.call_exception_handler({"message": "Task crashed", "exception": exc})
        assert "Asyncio unhandled exception: Task crashed (exc=Unhandled loop error)" in caplog.text
    finally:
        loop.close()


def test_check_block_page_detection():
    assert check_block_page("Please verify you are human to proceed", "https://carrier.com", 200, "Verify") == "marker: verify you are human"
    assert check_block_page("Access Denied on this server", "https://carrier.com", 403, "Access Denied") == "HTTP 403"
    assert check_block_page("Something else", "https://carrier.com", 429, "Rate limit") == "HTTP 429"
    assert check_block_page("Normal tracking details for PRO 123", "https://carrier.com", 200, "Track Shipment") is None
