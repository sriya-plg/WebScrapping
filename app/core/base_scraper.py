"""
The generic scraper engine.

This is the ONE place that knows how to drive a browser: launch with
stealth patching, navigate, fill a reference number, submit, intercept
the resulting network response, retry with backoff, detect blocks, and
log every stage. It is entirely config-driven (via CarrierConfig) and
knows nothing carrier-specific.

A carrier is fully defined by:
  1. its YAML config (app/config/carriers/<name>.yaml) -- WHERE things are
  2. its normalizer function (app/carriers/<name>/normalizer.py) -- WHAT
     the response JSON means

Adding carrier #4 should never require touching this file.
"""

import os
import random
import time
from json import JSONDecodeError
from typing import Callable

from playwright.sync_api import Frame, Page, sync_playwright, TimeoutError as PWTimeoutError
try:
    from playwright_stealth import stealth_sync
except ImportError:
    from playwright_stealth import Stealth

    def stealth_sync(page: Page) -> None:
        Stealth().apply_stealth_sync(page)

from app.config.settings import settings
from app.core.carrier_config import CarrierConfig
from app.core.exceptions import ScraperBlockedError, ScraperNotFoundError
from app.core.logger import get_logger
from app.core.models import TrackingJob, TrackingResult

BLOCK_MARKERS = ["captcha", "access denied", "checking your browser", "blocked", "rate limit"]

# Type alias: a normalizer takes (raw_json_payload, reference) -> TrackingResult
NormalizerFn = Callable[[dict, str], TrackingResult]


class GenericScraper:
    def __init__(self, config: CarrierConfig, normalize: NormalizerFn):
        self.config = config
        self.normalize = normalize
        self.logger = get_logger(f"scraper.{config.name}")

    # ---- Public API ----

    def track(self, reference: str) -> TrackingResult:
        return self._track_internal(reference, job=None)

    def track_job(self, job: TrackingJob) -> TrackingResult:
        """Same as track(), but driven by a full TrackingJob -- uses the
        job's own URL/credentials/reference_type rather than the carrier's
        default config values. This is the path the backend-driven flow
        uses once jobs start arriving as JSON."""
        result = self._track_internal(job.reference_number, job=job)
        result.bill_to_customer_code = job.bill_to_customer_code
        return result

    def _track_internal(self, reference: str, job: "TrackingJob | None") -> TrackingResult:
        safe_log_extra = {"carrier": self.config.name, "reference": reference}
        if job:
            # NEVER include job.password (or the job object itself) in any
            # log call -- this dict is explicitly built to exclude it.
            safe_log_extra["bill_to_customer_code"] = job.bill_to_customer_code
            safe_log_extra["reference_type"] = job.reference_type

        self.logger.info("track_start", extra=safe_log_extra)

        last_exception = None
        for attempt in range(1, settings.max_retries + 1):
            try:
                result = self._run_single_attempt(reference, attempt, job=job)
                self.logger.info(
                    "track_success",
                    extra={"carrier": self.config.name, "reference": reference, "attempt": attempt},
                )
                return result
            except ScraperBlockedError:
                self.logger.error(
                    "track_blocked_aborting",
                    extra={"carrier": self.config.name, "reference": reference, "attempt": attempt},
                )
                raise
            except ScraperNotFoundError as e:
                self.logger.warning(
                    "track_not_found",
                    extra={"carrier": self.config.name, "reference": reference, "reason": str(e)},
                )
                raise
            except Exception as e:
                last_exception = e
                self.logger.warning(
                    "track_attempt_failed",
                    extra={
                        "carrier": self.config.name,
                        "reference": reference,
                        "attempt": attempt,
                        "error": str(e),
                    },
                )
                if attempt < settings.max_retries:
                    backoff = (2 ** attempt) + random.uniform(0, 1)
                    self.logger.info(
                        "track_retry_backoff",
                        extra={"carrier": self.config.name, "reference": reference, "wait_seconds": round(backoff, 1)},
                    )
                    time.sleep(backoff)

        self.logger.error(
            "track_failed_all_retries",
            extra={"carrier": self.config.name, "reference": reference, "error": str(last_exception)},
        )
        raise last_exception

    def track_many(self, references: list[str]) -> list[TrackingResult]:
        """Sequential, deliberately not parallel -- see design notes on
        avoiding the volume spike that trips bot detection fastest."""
        self.logger.info("batch_start", extra={"carrier": self.config.name, "count": len(references)})
        results = []
        for i, ref in enumerate(references):
            try:
                results.append(self.track(ref))
            except ScraperBlockedError:
                self.logger.error(
                    "batch_aborted_block_detected",
                    extra={"carrier": self.config.name, "completed": i, "remaining": len(references) - i},
                )
                break
            except ScraperNotFoundError:
                continue
            except Exception as e:
                self.logger.error(
                    "batch_item_failed",
                    extra={"carrier": self.config.name, "reference": ref, "error": str(e)},
                )
                continue

            if i < len(references) - 1:
                delay = random.uniform(settings.min_delay_seconds, settings.max_delay_seconds)
                self.logger.debug(
                    "batch_pacing_delay",
                    extra={"carrier": self.config.name, "wait_seconds": round(delay, 1)},
                )
                time.sleep(delay)

        self.logger.info(
            "batch_complete",
            extra={"carrier": self.config.name, "succeeded": len(results), "attempted": len(references)},
        )
        return results

    def track_jobs(self, jobs: list[TrackingJob]) -> list[TrackingResult]:
        """Same sequential/paced batch behavior as track_many, but driven
        by full TrackingJob objects (backend-provided url/credentials/
        reference_type per job)."""
        self.logger.info("job_batch_start", extra={"carrier": self.config.name, "count": len(jobs)})
        results = []
        for i, job in enumerate(jobs):
            try:
                results.append(self.track_job(job))
            except ScraperBlockedError:
                self.logger.error(
                    "job_batch_aborted_block_detected",
                    extra={"carrier": self.config.name, "completed": i, "remaining": len(jobs) - i},
                )
                break
            except ScraperNotFoundError:
                continue
            except Exception as e:
                self.logger.error(
                    "job_batch_item_failed",
                    extra={
                        "carrier": self.config.name,
                        "bill_to_customer_code": job.bill_to_customer_code,
                        "reference": job.reference_number,
                        "error": str(e),
                    },
                )
                continue

            if i < len(jobs) - 1:
                delay = random.uniform(settings.min_delay_seconds, settings.max_delay_seconds)
                time.sleep(delay)

        self.logger.info(
            "job_batch_complete",
            extra={"carrier": self.config.name, "succeeded": len(results), "attempted": len(jobs)},
        )
        return results

    # ---- Internals ----

    def _run_single_attempt(self, reference: str, attempt: int, job: "TrackingJob | None" = None) -> TrackingResult:
        cfg = self.config
        target_url = job.url if job else cfg.tracking_url

        self.logger.debug(
            "browser_launch",
            extra={"carrier": cfg.name, "reference": reference, "attempt": attempt, "headless": settings.headless},
        )
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=settings.headless)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()
            stealth_sync(page)
            self.logger.debug("stealth_applied", extra={"carrier": cfg.name})

            # Force-patch navigator.webdriver, since stealth_sync alone isn't
            # reliably clearing it on current Chromium/Playwright versions.
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            browser_diagnostics = {
                "consent_accepted": False,
                "console_messages": [],
                "console_errors": [],
                "page_errors": [],
                "failed_requests": [],
                "failed_script_requests": [],
                "script_requests": [],
                "script_responses": [],
                "navigations": [],
            }

            def record_console(message) -> None:
                location = message.location or {}
                console_message = {
                    "text": message.text,
                    "type": message.type,
                    "url": location.get("url", ""),
                    "line": location.get("lineNumber"),
                    "column": location.get("columnNumber"),
                    "after_consent": browser_diagnostics["consent_accepted"],
                }
                browser_diagnostics["console_messages"].append(console_message)
                self.logger.info(
                    "browser_console_message",
                    extra={"carrier": cfg.name, **console_message},
                )
                if browser_diagnostics["consent_accepted"] and message.type == "error":
                    console_error = console_message
                    browser_diagnostics["console_errors"].append(console_error)
                    self.logger.warning(
                        "browser_console_error_after_cookie_consent",
                        extra={"carrier": cfg.name, **console_message},
                    )

            def record_page_error(error) -> None:
                page_error = {
                    "message": getattr(error, "message", str(error)),
                    "stack": getattr(error, "stack", ""),
                    "url": page.url,
                }
                browser_diagnostics["page_errors"].append(page_error)
                log_page_error = {
                    ("page_error_message" if key == "message" else key): value
                    for key, value in page_error.items()
                }
                self.logger.warning(
                    "browser_page_error_after_cookie_consent",
                    extra={"carrier": cfg.name, **log_page_error},
                )

            def record_response(response) -> None:
                if response.request.resource_type == "script":
                    script_response = {"url": response.url, "status": response.status}
                    browser_diagnostics["script_responses"].append(script_response)
                    self.logger.debug(
                        "browser_script_response",
                        extra={"carrier": cfg.name, **script_response},
                    )

            def record_request(request) -> None:
                if request.resource_type == "script":
                    browser_diagnostics["script_requests"].append(request.url)

            def record_failed_request(request) -> None:
                failed_request = {
                    "method": request.method,
                    "url": request.url,
                    "resource_type": request.resource_type,
                    "failure": request.failure,
                }
                browser_diagnostics["failed_requests"].append(failed_request)
                self.logger.warning(
                    "browser_request_failed",
                    extra={"carrier": cfg.name, **failed_request},
                )
                if request.resource_type == "script" or request.url.lower().split("?", 1)[0].endswith(".js"):
                    browser_diagnostics["failed_script_requests"].append(failed_request)
                    self.logger.warning(
                        "browser_script_request_failed",
                        extra={"carrier": cfg.name, **failed_request},
                    )

            def record_navigation(frame) -> None:
                navigation = {
                    "url": frame.url,
                    "is_main_frame": frame == page.main_frame,
                    "after_consent": browser_diagnostics["consent_accepted"],
                }
                browser_diagnostics["navigations"].append(navigation)
                if browser_diagnostics["consent_accepted"]:
                    self.logger.info(
                        "navigation_after_cookie_consent",
                        extra={"carrier": cfg.name, **navigation},
                    )

            page.on("console", record_console)
            page.on("pageerror", record_page_error)
            page.on("request", record_request)
            page.on("response", record_response)
            page.on("requestfailed", record_failed_request)
            page.on("framenavigated", record_navigation)

            try:
                self.logger.debug("navigating", extra={"carrier": cfg.name, "url": target_url})
                page.goto(target_url, wait_until=cfg.wait_until)

                if cfg.bot_protected:
                    self._check_for_block(page)

                self._handle_cookie_consent(
                    page,
                    on_accepted=lambda: browser_diagnostics.__setitem__("consent_accepted", True),
                )
                if browser_diagnostics["consent_accepted"]:
                    self.logger.info(
                        "page_state_after_cookie_acceptance",
                        extra={
                            "carrier": cfg.name,
                            "page_url": page.url,
                            "document_ready_state": page.evaluate("document.readyState"),
                            "criteria_count": page.locator(cfg.reference_input_selector).count(),
                            "criteria_visible": page.locator(cfg.reference_input_selector).first.is_visible(timeout=500)
                            if page.locator(cfg.reference_input_selector).count()
                            else False,
                            "script_tag_urls": page.locator("script[src]").evaluate_all(
                                "scripts => scripts.map(script => script.src)"
                            ),
                        },
                    )
                self._wait_after_cookie_consent(page, browser_diagnostics)
                self._log_post_consent_state(page, browser_diagnostics)
                self._wait_for_angular_bootstrap(page, browser_diagnostics)
                self.logger.debug(
                    "tracking_form_wait_started",
                    extra={"carrier": cfg.name, "selector": cfg.reference_input_selector},
                )
                form = self._find_form_context(page, cfg.reference_input_selector)

                if cfg.login.required:
                    if not job:
                        raise RuntimeError(
                            f"{cfg.name} config requires login but no TrackingJob "
                            f"(with credentials) was provided -- use track_job(), not track()."
                        )
                    self._do_login(form, job)

                if cfg.reference_type_field.selector and job:
                    self._select_reference_type(form, job)

                self._fill_reference(form, cfg.reference_input_selector, reference)

                def submit():
                    self._click_submit(
                        form,
                        cfg.submit_button_selector,
                        cfg.reference_input_selector,
                        reference,
                    )

                payload = self._capture_json_response(
                    page,
                    cfg.api_url_substring,
                    submit,
                    fallback_url=cfg.api_url_template.format(reference=reference)
                    if cfg.api_url_template
                    else None,
                )
                self.logger.debug("response_parsed", extra={"carrier": cfg.name})

                result = self.normalize(payload, reference)
                return result

            except PWTimeoutError as e:
                self._check_for_block(page)
                raise RuntimeError(f"Timeout waiting for page state: {e}")
            finally:
                page.remove_listener("console", record_console)
                page.remove_listener("pageerror", record_page_error)
                page.remove_listener("request", record_request)
                page.remove_listener("response", record_response)
                page.remove_listener("requestfailed", record_failed_request)
                page.remove_listener("framenavigated", record_navigation)
                browser.close()
                self.logger.debug("browser_closed", extra={"carrier": cfg.name})

    def _handle_cookie_consent(self, page: Page, on_accepted=None) -> None:
        selector = self.config.cookie_consent.accept_selector
        if not selector:
            return

        self.logger.debug(
            "cookie_consent_configured",
            extra={"carrier": self.config.name, "selector": selector},
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for frame in page.frames:
                try:
                    accept_button = frame.locator(selector).first
                    if not accept_button.is_visible(timeout=250):
                        continue
                    self.logger.info(
                        "cookie_consent_detected",
                        extra={"carrier": self.config.name, "frame_url": frame.url},
                    )
                    if on_accepted:
                        on_accepted()
                    expect_navigation = getattr(page, "expect_navigation", None)
                    if expect_navigation:
                        try:
                            with expect_navigation(
                                wait_until="domcontentloaded",
                                timeout=5_000,
                            ):
                                accept_button.click()
                            self.logger.info(
                                "cookie_consent_navigation_completed",
                                extra={"carrier": self.config.name, "url": page.url},
                            )
                        except PWTimeoutError:
                            self.logger.debug(
                                "cookie_consent_navigation_not_observed",
                                extra={"carrier": self.config.name, "url": page.url},
                            )
                    else:
                        accept_button.click()
                    try:
                        accept_button.wait_for(state="hidden", timeout=2_000)
                    except PWTimeoutError:
                        self.logger.debug(
                            "cookie_consent_dismissal_not_confirmed",
                            extra={"carrier": self.config.name, "frame_url": frame.url},
                        )
                    self.logger.info(
                        "cookie_consent_accepted",
                        extra={"carrier": self.config.name, "frame_url": frame.url},
                    )
                    return
                except PWTimeoutError:
                    continue
                except Exception as exc:
                    self.logger.debug(
                        "cookie_consent_click_failed",
                        extra={"carrier": self.config.name, "selector": selector, "error": str(exc)},
                    )
            page.wait_for_timeout(250)

        self.logger.debug(
            "cookie_consent_not_present",
            extra={"carrier": self.config.name, "selector": selector},
        )

    def _wait_after_cookie_consent(self, page: Page, diagnostics: dict) -> None:
        if not diagnostics["consent_accepted"]:
            return
        if any(
            navigation["is_main_frame"] and navigation["after_consent"]
            for navigation in diagnostics["navigations"]
        ):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=3_000)
                self.logger.debug(
                    "navigation_after_cookie_consent_complete",
                    extra={"carrier": self.config.name, "url": page.url},
                )
            except PWTimeoutError:
                self.logger.warning(
                    "navigation_after_cookie_consent_incomplete",
                    extra={"carrier": self.config.name, "url": page.url},
                )

    def _log_post_consent_state(
        self,
        page: Page,
        diagnostics: dict,
        event_name: str = "post_cookie_consent_page_state",
    ) -> None:
        app_root = page.locator("app-root")
        try:
            app_root_count = app_root.count()
            app_root_inner_html = app_root.first.inner_html(timeout=1_000) if app_root_count else ""
            app_root_child_count = app_root.first.locator(":scope > *").count() if app_root_count else 0
        except Exception as exc:
            app_root_count = 0
            app_root_inner_html = f"<unavailable: {exc}>"
            app_root_child_count = 0
        self.logger.info(
            event_name,
            extra={
                "carrier": self.config.name,
                "page_url": page.url,
                "page_title": page.title(),
                "document_ready_state": page.evaluate("document.readyState"),
                "app_root_count": app_root_count,
                "app_root_child_count": app_root_child_count,
                "app_root_inner_html": app_root_inner_html,
                "console_errors": diagnostics["console_errors"],
                "console_messages": diagnostics["console_messages"],
                "page_errors": diagnostics["page_errors"],
                "failed_requests": diagnostics["failed_requests"],
                "failed_script_requests": diagnostics["failed_script_requests"],
                "script_requests": diagnostics["script_requests"],
                "script_responses": diagnostics["script_responses"],
                "navigations": diagnostics["navigations"],
            },
        )

    def _wait_for_angular_bootstrap(self, page: Page, diagnostics: dict) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(
                frame.locator(self.config.reference_input_selector).count()
                for frame in page.frames
            ):
                self.logger.debug(
                    "tracking_form_bootstrapped",
                    extra={"carrier": self.config.name, "selector": self.config.reference_input_selector},
                )
                return
            page.wait_for_timeout(250)
        self.logger.warning(
            "angular_app_bootstrap_not_observed",
            extra={"carrier": self.config.name, "url": page.url},
        )
        self._log_post_consent_state(
            page,
            diagnostics,
            event_name="angular_bootstrap_failure_state",
        )

    def _fill_reference(self, page: Page | Frame, selector: str, reference: str) -> None:
        field = page.locator(selector).first
        field.wait_for(state="visible", timeout=5_000)
        self.logger.debug(
            "filling_reference_field",
            extra={"carrier": self.config.name, "reference": reference, "selector": selector},
        )
        field.fill(reference)
        actual_value = field.input_value()
        if actual_value != reference:
            raise RuntimeError(
                f"Reference field {selector!r} contains {actual_value!r} after filling; "
                f"expected {reference!r}."
            )
        self.logger.debug(
            "reference_field_verified",
            extra={"carrier": self.config.name, "reference": reference, "selector": selector},
        )

    def _click_submit(
        self,
        page: Page | Frame,
        selector: str,
        reference_selector: str = "",
        reference: str = "",
    ) -> None:
        if reference_selector:
            reference_field = page.locator(reference_selector).first
            reference_field.wait_for(state="visible", timeout=5_000)
            actual_value = reference_field.input_value()
            if actual_value != reference:
                self.logger.warning(
                    "reference_field_cleared_before_submit_refilling",
                    extra={"carrier": self.config.name, "actual_value": actual_value, "expected": reference},
                )
                reference_field.fill(reference)
                actual_value = reference_field.input_value()
                if actual_value != reference:
                    raise RuntimeError(
                        f"Reference field {reference_selector!r} contains {actual_value!r} "
                        f"immediately before submit even after re-fill; expected {reference!r}."
                    )
        button = page.locator(selector).first
        button.wait_for(state="visible", timeout=5_000)
        if not button.is_enabled():
            raise RuntimeError(f"Submit button {selector!r} is visible but disabled.")
        self.logger.debug(
            "clicking_submit",
            extra={
                "carrier": self.config.name,
                "selector": selector,
                "visible": True,
                "enabled": True,
            },
        )
        button.click()
        self.logger.debug(
            "submit_click_completed",
            extra={"carrier": self.config.name, "selector": selector},
        )


    def _find_form_context(self, page: Page, selector: str) -> Page | Frame:
        """Find the tracking form in the document or an embedded frame.

        Some carrier sites embed their public tracking widget in an iframe.
        Playwright selectors do not cross frame boundaries automatically, so
        discover the frame before filling or clicking the form controls.

        The polling uses Playwright's wait_for_timeout() instead of
        time.sleep() so that browser/page events and dynamically-rendered
        JavaScript applications have a chance to progress.
        """
        deadline = time.monotonic() + 30

        while time.monotonic() < deadline:
            for frame in page.frames:
                try:
                    locator = frame.locator(selector).first

                    # Check whether the element has been attached/rendered.
                    # A short wait prevents us from repeatedly querying too fast
                    # while Angular/React/etc. is still rendering the page.
                    locator.wait_for(
                        state="visible",
                        timeout=500,
                    )

                    self.logger.debug(
                        "tracking_form_found",
                        extra={
                            "carrier": self.config.name,
                            "frame_url": frame.url,
                            "selector": selector,
                        },
                    )

                    return frame

                except PWTimeoutError:
                    continue
                except Exception as exc:
                    self.logger.debug(
                        "tracking_form_check_failed",
                        extra={
                            "carrier": self.config.name,
                            "frame_url": frame.url,
                            "selector": selector,
                            "error": str(exc),
                        },
                    )
                    continue

            # IMPORTANT:
            # Use Playwright's wait instead of time.sleep().
            # This allows browser events and dynamically-rendered DOM changes
            # to continue being processed.
            page.wait_for_timeout(250)

        # -----------------------------
        # Diagnostics after timeout
        # -----------------------------
        frame_urls = [frame.url for frame in page.frames]
        try:
            page_title = page.title()
        except Exception as exc:
            page_title = f"<unavailable: {exc}>"
        try:
            body_text = page.locator("body").inner_text(timeout=1_000)
        except Exception as exc:
            body_text = f"<unavailable: {exc}>"

        os.makedirs(settings.log_dir, exist_ok=True)

        diagnostic_path = os.path.join(
            settings.log_dir,
            f"{self.config.name}_form_failure.html",
        )

        with open(diagnostic_path, "w", encoding="utf-8") as diagnostic_file:
            diagnostic_file.write(page.content())

        screenshot_path = os.path.join(
            settings.log_dir,
            f"{self.config.name}_form_failure.png",
        )

        page.screenshot(
            path=screenshot_path,
            full_page=True,
        )

        self.logger.warning(
            "tracking_form_missing_diagnostics",
            extra={
                "carrier": self.config.name,
                "page_url": page.url,
                "page_title": page_title,
                "body_text": body_text,
                "screenshot_path": screenshot_path,
                "selector": selector,
            },
        )

        # Extra diagnostic information: tell us whether the selector exists
        # but is simply not visible.
        selector_counts = {}

        for frame in page.frames:
            try:
                selector_counts[frame.url] = frame.locator(selector).count()
            except Exception:
                selector_counts[frame.url] = "error"

        raise RuntimeError(
            f"Tracking form selector {selector!r} was not found after 30 seconds. "
            f"Frames inspected: {frame_urls}. "
            f"Selector counts: {selector_counts}. "
            f"Saved the page to {diagnostic_path} and screenshot to "
            f"{screenshot_path}."
        )

    def _do_login(self, page: Page | Frame, job: "TrackingJob") -> None:
        """Fills and submits the login form using job-supplied credentials.
        Credentials are used in-memory only -- this method (and everything
        it calls) must never pass job.password into a log call."""
        cfg = self.config
        self.logger.debug(
            "login_start",
            extra={"carrier": cfg.name, "bill_to_customer_code": job.bill_to_customer_code},
        )
        page.fill(cfg.login.username_input, job.username)
        page.fill(cfg.login.password_input, job.password)  # not logged
        page.click(cfg.login.submit_button)
        page.wait_for_load_state(cfg.wait_until)
        self.logger.debug(
            "login_complete",
            extra={"carrier": cfg.name, "bill_to_customer_code": job.bill_to_customer_code},
        )

    def _select_reference_type(self, page: Page | Frame, job: "TrackingJob") -> None:
        cfg = self.config
        option_value = cfg.reference_type_field.options.get(job.reference_type.lower())
        if not option_value:
            raise RuntimeError(
                f"{cfg.name} config has no mapped option for reference_type "
                f"'{job.reference_type}'. Known types: {list(cfg.reference_type_field.options)}"
            )
        self.logger.debug(
            "selecting_reference_type",
            extra={"carrier": cfg.name, "reference_type": job.reference_type},
        )
        page.select_option(cfg.reference_type_field.selector, option_value)


    def _capture_json_response(
        self,
        page: Page,
        url_substring: str,
        trigger,
        fallback_url: str | None = None,
    ) -> dict:
        """Trigger the browser action and capture the matching network response.

        The request is made by the real browser page. This method only observes
        the request/response generated by the page.

        We record both requests and responses so that a timeout can distinguish
        between:
        - the expected request never being sent,
        - the request being sent but failing,
        - the request being sent with a non-JSON response, or
        - the expected response not arriving in time.
        """
        self.logger.debug(
            "waiting_for_response",
            extra={
                "carrier": self.config.name,
                "url_substring": url_substring,
            },
        )

        observed_requests: list[str] = []
        observed_responses: list[str] = []
        failed_requests: list[str] = []
        search_triggered = False

        def record_request(request) -> None:
            observed_requests.append(
                f"{request.method} {request.url}"
            )

            self.logger.debug(
                "api_request_observed",
                extra={
                    "carrier": self.config.name,
                    "method": request.method,
                    "url": request.url,
                    "resource_type": request.resource_type,
                },
            )
            if search_triggered and request.resource_type in ("fetch", "xhr") and url_substring in request.url:
                try:
                    request_payload = request.post_data
                except Exception:
                    request_payload = None
                self.logger.info(
                    "tracking_fetch_xhr_observed",
                    extra={
                        "carrier": self.config.name,
                        "method": request.method,
                        "url": request.url,
                        "resource_type": request.resource_type,
                        "expected_url_substring": url_substring,
                        "matches_expected_url": url_substring in request.url,
                        "request_payload": request_payload,
                    },
                )

        def record_response(response) -> None:
            observed_responses.append(
                f"{response.status} {response.url}"
            )

            self.logger.debug(
                "api_response_observed",
                extra={
                    "carrier": self.config.name,
                    "status": response.status,
                    "url": response.url,
                    "resource_type": response.request.resource_type,
                },
            )
            if search_triggered and response.request.resource_type in ("fetch", "xhr") and url_substring in response.url:
                try:
                    response_body = response.text()
                except Exception:
                    response_body = None
                self.logger.info(
                    "tracking_fetch_xhr_response",
                    extra={
                        "carrier": self.config.name,
                        "status": response.status,
                        "url": response.url,
                        "resource_type": response.request.resource_type,
                        "response_body": response_body,
                    },
                )

        def record_failed_request(request) -> None:
            failed_requests.append(
                f"{request.method} {request.url}: {request.failure}"
            )

        page.on("request", record_request)
        page.on("response", record_response)
        page.on("requestfailed", record_failed_request)

        try:
            with page.expect_response(
                lambda response: url_substring in response.url,
                timeout=settings.response_timeout_ms,
            ) as response_info:

                self.logger.debug(
                    "triggering_tracking_search",
                    extra={
                        "carrier": self.config.name,
                        "expected_url_substring": url_substring,
                    },
                )

                search_triggered = True
                trigger()

            response = response_info.value

        except PWTimeoutError as error:
            matching_requests = [
                url
                for url in observed_requests
                if url_substring in url
            ]

            matching_responses = [
                url
                for url in observed_responses
                if url_substring in url
            ]

            self.logger.warning(
                "expected_api_response_not_seen",
                extra={
                    "carrier": self.config.name,
                    "expected_url_substring": url_substring,
                    "matching_requests": matching_requests,
                    "matching_responses": matching_responses,
                    "expected_request_match_found": bool(matching_requests),
                    "observed_requests": observed_requests,
                    "observed_responses": observed_responses,
                    "failed_requests": failed_requests,
                },
            )

            if fallback_url:
                fallback_response = page.request.get(fallback_url)
                fallback_body = fallback_response.text()
                self.logger.info(
                    "tracking_api_transaction",
                    extra={
                        "carrier": self.config.name,
                        "method": "GET",
                        "url": fallback_url,
                        "request_payload": None,
                        "status": fallback_response.status,
                        "response_body": fallback_body,
                        "source": "configured_fallback",
                    },
                )
                if fallback_response.status >= 400:
                    raise RuntimeError(
                        f"HTTP {fallback_response.status} on fallback {fallback_url}"
                    )
                try:
                    return fallback_response.json()
                except (JSONDecodeError, ValueError) as fallback_error:
                    raise RuntimeError(
                        f"Fallback tracking API returned non-JSON content: {fallback_url}"
                    ) from fallback_error

            raise RuntimeError(
                f"No matching API response for {url_substring!r}. "
                f"Matching requests: {matching_requests}. "
                f"Matching responses: {matching_responses}. "
                f"Observed requests: {observed_requests}. "
                f"Failed requests: {failed_requests}"
            ) from error

        finally:
            page.remove_listener("request", record_request)
            page.remove_listener("response", record_response)
            page.remove_listener("requestfailed", record_failed_request)

        self.logger.debug(
            "response_captured",
            extra={
                "carrier": self.config.name,
                "url": response.url,
                "status": response.status,
            },
        )

        try:
            request_payload = response.request.post_data
        except Exception:
            request_payload = None
        self.logger.info(
            "tracking_api_transaction",
            extra={
                "carrier": self.config.name,
                "method": response.request.method,
                "url": response.url,
                "request_payload": request_payload,
                "status": response.status,
            },
        )

        if response.status == 403:
            raise ScraperBlockedError(
                f"403 on {response.url}"
            )

        if response.status == 429:
            raise ScraperBlockedError(
                f"429 rate-limited on {response.url}"
            )

        if response.status >= 400:
            raise RuntimeError(
                f"HTTP {response.status} on {response.url}"
            )

        if response.status == 204:
            self.logger.info(
                "empty_api_response",
                extra={
                    "carrier": self.config.name,
                    "url": response.url,
                },
            )
            return {"data": []}

        try:
            return response.json()

        except (JSONDecodeError, ValueError) as error:
            # Some carriers acknowledge the request but don't return JSON.
            # Preserve the browser state for diagnosis.
            page.wait_for_timeout(1_000)

            os.makedirs(settings.log_dir, exist_ok=True)

            diagnostic_path = os.path.join(
                settings.log_dir,
                f"{self.config.name}_tracking_page.html",
            )

            with open(
                diagnostic_path,
                "w",
                encoding="utf-8",
            ) as diagnostic_file:
                diagnostic_file.write(page.content())

            screenshot_path = os.path.join(
                settings.log_dir,
                f"{self.config.name}_tracking_page.png",
            )

            page.screenshot(
                path=screenshot_path,
                full_page=True,
            )

            raise RuntimeError(
                f"The tracking form returned HTTP {response.status} "
                f"with no JSON body. "
                f"Saved the post-search page to {diagnostic_path} "
                f"and screenshot to {screenshot_path}."
            ) from error


    def _check_for_block(self, page: Page) -> None:
        content = page.content().lower()
        for marker in BLOCK_MARKERS:
            if marker in content:
                if marker == "captcha":
                    diagnostics = self._log_captcha_diagnostics(page)
                    if not diagnostics["active_challenge"]:
                        self.logger.info(
                            "recaptcha_widget_not_blocking",
                            extra={
                                "carrier": self.config.name,
                                "page_url": page.url,
                            },
                        )
                        continue
                self.logger.error(
                    "block_marker_detected",
                    extra={"carrier": self.config.name, "marker": marker, "url": page.url},
                )
                raise ScraperBlockedError(f"Block marker '{marker}' found on page")

    def _log_captcha_diagnostics(self, page: Page) -> dict:
        page_url = page.url
        page_title = page.title()
        document_ready_state = page.evaluate("document.readyState")
        recaptcha_frames = page.locator("iframe").evaluate_all(
            """iframes => iframes
                .filter(frame => /recaptcha/i.test(`${frame.src} ${frame.title}`))
                .map(frame => {
                    const rect = frame.getBoundingClientRect();
                    const style = getComputedStyle(frame);
                    return {
                        src: frame.src,
                        title: frame.title,
                        width: rect.width,
                        height: rect.height,
                        visibility: style.visibility,
                        display: style.display,
                        visible: rect.width > 0 && rect.height > 0 &&
                            style.visibility !== "hidden" && style.display !== "none"
                    };
                })"""
        )
        checkbox_details = {}
        for frame in page.frames:
            if "recaptcha" not in frame.url.lower():
                continue
            if "/recaptcha/api2/anchor" in frame.url:
                try:
                    checkbox_details = frame.locator("#recaptcha-anchor").evaluate(
                        """element => ({
                            aria_label: element.getAttribute("aria-label") ||
                                document.getElementById(element.getAttribute("aria-labelledby"))?.innerText || "",
                            aria_checked: element.getAttribute("aria-checked"),
                            text: element.innerText || element.textContent || "",
                            visible: element.getClientRects().length > 0
                        })""",
                        timeout=500,
                    )
                except Exception:
                    pass
                break

        try:
            body_text = page.locator("body").inner_text(timeout=500)
        except Exception:
            body_text = ""

        captcha_text = (
            body_text.lower()
            + checkbox_details.get("aria_label", "").lower()
            + checkbox_details.get("text", "").lower()
        )
        robot_text_present = "i'm not a robot" in captcha_text
        body_text_lower = body_text.lower()
        excerpt_position = next(
            (
                body_text_lower.find(term)
                for term in (
                    "captcha",
                    "recaptcha",
                    "robot",
                    "challenge",
                    "verification",
                    "enter up to",
                    "track a shipment",
                )
                if body_text_lower.find(term) >= 0
            ),
            0,
        )
        body_text_excerpt = body_text[
            max(0, excerpt_position - 250):excerpt_position + 500
        ].strip()

        os.makedirs(settings.log_dir, exist_ok=True)
        html_snapshot_path = os.path.join(
            settings.log_dir, f"{self.config.name}_captcha_page.html"
        )
        screenshot_path = os.path.join(
            settings.log_dir, f"{self.config.name}_captcha_page.png"
        )
        snapshot_errors = []
        try:
            with open(html_snapshot_path, "w", encoding="utf-8") as snapshot_file:
                snapshot_file.write(page.content())
        except Exception as error:
            html_snapshot_path = ""
            snapshot_errors.append(f"HTML snapshot: {error}")
        try:
            page.screenshot(path=screenshot_path, full_page=True)
        except Exception as error:
            screenshot_path = ""
            snapshot_errors.append(f"Screenshot: {error}")

        form_state = self._inspect_form_controls(page)

        recaptcha_iframe_exists = bool(recaptcha_frames)
        google_recaptcha_checkbox_widget = any(
            "/recaptcha/api2/anchor" in frame["src"] and frame["visible"]
            for frame in recaptcha_frames
        )
        visible_bframes = [
            frame for frame in recaptcha_frames
            if "/recaptcha/api2/bframe" in frame["src"] and frame["visible"]
        ]
        active_challenge = bool(visible_bframes)
        checkbox_state = {
            "true": "checked",
            "false": "unchecked",
        }.get(checkbox_details.get("aria_checked"), "unknown")
        diagnostic_message = "\n".join(
            (
                "captcha_page_diagnostics",
                f"page_url={page_url}",
                f"page_title={page_title!r}",
                f"document_ready_state={document_ready_state}",
                f"page_fully_loaded={document_ready_state == 'complete'}",
                f"tracking_form_visible={form_state['form_visible']}",
                f"tracking_form_interactable={form_state['form_interactable']}",
                f"form_selector_matches={form_state['controls']!r}",
                f"recaptcha_iframe_exists={recaptcha_iframe_exists}",
                f"recaptcha_iframes={recaptcha_frames!r}",
                f"visible_bframe_iframes={visible_bframes!r}",
                f"active_recaptcha_challenge={active_challenge}",
                f"recaptcha_checkbox_label={checkbox_details.get('aria_label', '')!r}",
                f"recaptcha_checkbox_state={checkbox_state}",
                f"im_not_a_robot_text_present={robot_text_present}",
                f"captcha_body_text_excerpt={body_text_excerpt!r}",
                f"html_snapshot_path={html_snapshot_path or 'unavailable'}",
                f"screenshot_path={screenshot_path or 'unavailable'}",
                f"snapshot_errors={snapshot_errors!r}",
            )
        )
        self.logger.warning(
            diagnostic_message,
            extra={
                "carrier": self.config.name,
                "page_url": page_url,
                "page_title": page_title,
                "document_ready_state": document_ready_state,
                "page_fully_loaded": document_ready_state == "complete",
                "tracking_form_visible": form_state["form_visible"],
                "tracking_form_interactable": form_state["form_interactable"],
                "form_selector_matches": form_state["controls"],
                "recaptcha_frames": recaptcha_frames,
                "recaptcha_iframe_exists": recaptcha_iframe_exists,
                "google_recaptcha_checkbox_widget": google_recaptcha_checkbox_widget,
                "visible_bframe_iframes": visible_bframes,
                "active_recaptcha_challenge": active_challenge,
                "recaptcha_checkbox_element_present": bool(checkbox_details),
                "recaptcha_checkbox_label": checkbox_details.get("aria_label", ""),
                "recaptcha_checkbox_state": checkbox_state,
                "im_not_a_robot_text_present": robot_text_present,
                "captcha_body_text_excerpt": body_text_excerpt,
                "html_snapshot_path": html_snapshot_path,
                "screenshot_path": screenshot_path,
                "snapshot_errors": snapshot_errors,
            },
        )
        return {
            "widget_present": recaptcha_iframe_exists,
            "active_challenge": active_challenge,
            **form_state,
        }

    def _inspect_form_controls(self, page: Page) -> dict:
        controls = []
        for control_name, selector in (
            ("reference_input", self.config.reference_input_selector),
            ("submit_button", self.config.submit_button_selector),
        ):
            for frame in page.frames:
                try:
                    locator = frame.locator(selector)
                    count = locator.count()
                except Exception as error:
                    controls.append(
                        {
                            "control": control_name,
                            "selector": selector,
                            "frame_url": frame.url,
                            "in_iframe": frame != page.main_frame,
                            "match_count": 0,
                            "matches": [],
                            "error": str(error),
                        }
                    )
                    continue

                matches = []
                for index in range(count):
                    item = locator.nth(index)
                    try:
                        visible = item.is_visible()
                        enabled = item.is_enabled()
                        bounding_box = item.bounding_box()
                        in_shadow_dom = item.evaluate(
                            "element => element.getRootNode() instanceof ShadowRoot"
                        )
                        hit_target = item.evaluate(
                            """element => {
                                const rect = element.getBoundingClientRect();
                                const target = element.ownerDocument.elementFromPoint(
                                    rect.x + rect.width / 2,
                                    rect.y + rect.height / 2
                                );
                                return target === element || element.contains(target);
                            }"""
                        ) if visible and enabled else False
                    except Exception:
                        visible = False
                        enabled = False
                        bounding_box = None
                        in_shadow_dom = False
                        hit_target = False
                    matches.append(
                        {
                            "visible": visible,
                            "enabled": enabled,
                            "interactable": visible and enabled and hit_target,
                            "bounding_box": bounding_box,
                            "in_shadow_dom": in_shadow_dom,
                        }
                    )
                controls.append(
                    {
                        "control": control_name,
                        "selector": selector,
                        "frame_url": frame.url,
                        "in_iframe": frame != page.main_frame,
                        "match_count": count,
                        "matches": matches,
                    }
                )

        reference_ready = any(
            match["interactable"]
            for control in controls
            if control["control"] == "reference_input"
            for match in control["matches"]
        )
        submit_ready = any(
            match["interactable"]
            for control in controls
            if control["control"] == "submit_button"
            for match in control["matches"]
        )
        return {
            "controls": controls,
            "form_visible": any(
                match["visible"]
                for control in controls
                if control["control"] == "reference_input"
                for match in control["matches"]
            ),
            "form_interactable": reference_ready and submit_ready,
        }
