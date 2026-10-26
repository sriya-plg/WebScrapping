"""Network-response batch scraper driven entirely by carrier YAML configuration."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from app.browser.base import BrowserProvider, BrowserSession, CapturedResponse
from app.core.adapter import DEFAULT_CONSENT_SELECTORS, session_key
from app.core.login import execute_login
from app.core.mapper import get_path
from app.logging_setup import carrier_var, client_var, log
from app.models import TrackingJob
from app.settings import CarrierSpec

logger = logging.getLogger(__name__)

CaptureOutcome = str  # found | not_found | captured


class ChallengeError(RuntimeError):
    """Bot-check or Cloudflare challenge detected."""


class ScrapeConfigError(RuntimeError):
    """Required per-carrier selectors or response paths are not configured."""


@dataclass
class ScrapeBatchResult:
    requested: list[str]
    received: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    outcomes: dict[str, CaptureOutcome] = field(default_factory=dict)
    summaries: dict[str, dict[str, Any]] = field(default_factory=dict)

    def scrape_items(self) -> list[dict[str, Any]]:
        """Per-PRO raw carrier JSON for a future map/post step."""
        return [
            {
                "search_value": pro,
                "raw": raw,
                "outcome": self.outcomes.get(pro, "captured"),
                "summary": self.summaries.get(pro, {}),
            }
            for pro, raw in self.received.items()
        ]


def classify_carrier_raw(raw: Any, spec: CarrierSpec) -> tuple[CaptureOutcome, dict[str, Any]]:
    """Classify captured carrier JSON (including empty data + error = not_found)."""
    extra = spec.site.extra or {}
    data_path = extra.get("response_data_path", "data")
    err_msg_path = extra.get("response_error_message_path", "error.message")
    err_code_path = extra.get("response_error_code_path", "error.code")
    not_found_codes = {str(c) for c in (extra.get("response_not_found_codes") or [])}

    if isinstance(raw, list):
        summary: dict[str, Any] = {"data_count": len(raw)}
        if not raw:
            return "not_found", summary
        return "found", summary
    if isinstance(raw, dict) and "." not in data_path and data_path in raw:
        data_val = raw[data_path]
    else:
        data_val = get_path(raw, data_path, default=None)
    err_msg = get_path(raw, err_msg_path)
    err_code = get_path(raw, err_code_path)
    summary = {
        "error_code": err_code,
        "error_message": err_msg,
    }
    if isinstance(raw, dict) and not (err_msg or err_code is not None):
        if get_path(raw, "shipment") is not None or get_path(raw, "referenceNumbers") is not None:
            summary["data_count"] = 1
            return "found", summary
    if isinstance(data_val, list):
        summary["data_count"] = len(data_val)
        if data_val:
            return "found", summary
        if err_msg or err_code is not None:
            if not_found_codes and err_code is not None and str(err_code) not in not_found_codes:
                return "captured", summary
            return "not_found", summary
    if err_msg or err_code is not None:
        return "not_found", summary
    return "captured", summary


class Scraper(Protocol):
    async def run(self, job: TrackingJob) -> ScrapeBatchResult: ...


def _path_values_any(obj: Any, path: str) -> list[Any]:
    """Like _path_values but `a|b` tries each alternative."""
    found: list[Any] = []
    for alt in str(path or "").split("|"):
        alt = alt.strip()
        if alt:
            found.extend(_path_values(obj, alt))
    return found


def _path_values(obj: Any, path: str) -> list[Any]:
    """Resolve dotted JSON paths with numeric indexes and []/[*] list expansion."""
    path = path.removeprefix("$").lstrip(".")
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]|\[\*\]|\[\]", path)
    current = [obj]
    for token in tokens:
        following = []
        if token in ("[]", "[*]"):
            following.extend(child for value in current if isinstance(value, list) for child in value)
        elif token.startswith("["):
            index = int(token[1:-1])
            following.extend(value[index] for value in current if isinstance(value, list) and index < len(value))
        elif token.isdigit():
            index = int(token)
            following.extend(value[index] for value in current if isinstance(value, list) and index < len(value))
        else:
            for value in current:
                if isinstance(value, dict) and token in value:
                    following.append(value[token])
                elif isinstance(value, list):
                    following.extend(child[token] for child in value if isinstance(child, dict) and token in child)
        current = following
    return current


def _pro_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def format_pro_for_input(pro: str, spec: CarrierSpec) -> str:
    """Format a PRO for the carrier search box (may differ from backend storage)."""
    cleaned = str(pro or "").strip()
    if spec.site.strip_spaces_dashes:
        cleaned = re.sub(r"[\s-]+", "", cleaned)
    digits = _pro_digits(cleaned)
    input_len = spec.site.extra.get("pro_input_digits")
    if input_len and digits.isdigit():
        il = int(input_len)
        if len(digits) > il:
            digits = digits[:il]
        elif len(digits) == il + 1:
            digits = digits[:il]
    fmt = spec.site.extra.get("pro_input_format")
    if fmt == "estes_dashed" and len(digits) == 10:
        return f"{digits[:3]}-{digits[3:]}"
    if digits and cleaned == _pro_digits(cleaned):
        return digits
    return cleaned


def normalize_pro(value: Any, spec: CarrierSpec) -> str:
    result = str(value or "").strip()
    if spec.site.strip_spaces_dashes:
        result = re.sub(r"[\s-]+", "", result)
    digits = _pro_digits(result)
    input_len = spec.site.extra.get("pro_input_digits")
    if input_len and digits.isdigit():
        il = int(input_len)
        if len(digits) > il:
            result = digits[:il]
        elif len(digits) >= il:
            result = digits[:il]
    elif spec.site.strip_leading_zeros:
        result = result.lstrip("0") or "0"
    return result.casefold()


def _chunks(items: list[TrackingJob], size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _tracking_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]


def _normalize_tracking_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _safe_url(url: str, secrets: list[str]) -> str:
    parts = urlsplit(url)
    query = [(key, "REDACTED" if re.search(r"password|token|auth|key|secret|cookie|credential|session", key, re.I) else value)
             for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    safe = urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))
    for secret in secrets:
        safe = safe.replace(secret, "REDACTED")
    return safe


def _response_url_without_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class BatchScraper:
    def __init__(self, provider: BrowserProvider, spec: CarrierSpec | None = None,
                 debug_dir: str = "state/debug"):
        self.provider, self.spec = provider, spec
        self.debug_dir = Path(debug_dir)
        self._diagnostic_index = 0

    async def run(self, job: TrackingJob) -> ScrapeBatchResult:
        if self.spec is None:
            raise ValueError("CarrierSpec is required for BatchScraper.run")
        return await self.run_batch([job], self.spec)

    async def run_batch(self, jobs: list[TrackingJob], spec: CarrierSpec) -> ScrapeBatchResult:
        result = ScrapeBatchResult(requested=[job.search_value for job in jobs])
        if not jobs:
            return result
        site = spec.site
        required = {"input_selector": site.input_selector,
                    "submit_selector": site.submit_selector,
                    "response_url_pattern": site.response_url_pattern or site.response_regex,
                    "response_pro_path": site.response_pro_path}
        missing_config = [name for name, value in required.items()
                          if not value or "TODO" in value.upper()]
        if missing_config:
            result.missing = list(result.requested)
            result.errors.append({"type": "ScrapeConfigError", "message":
                                  "replace carrier TODO values: " + ", ".join(missing_config)})
            return result
        session: BrowserSession | None = None
        config = jobs[0].carrier
        secrets = [value for value in (config.user_name,
                    config.password.get_secret_value() if config.password else None) if value]
        try:
            log(logger, logging.INFO, "browser session opening",
                engine=spec.browser.browser_mode or spec.browser.provider,
                headless=spec.browser.headless, channel=spec.browser.channel or "bundled chromium",
                tracking_url=_safe_url(site.tracking_url or config.tracking_url or "", secrets))
            client_var.set(spec.client)
            carrier_var.set(spec.code)
            session = await self.provider.open_session(session_key(spec))
            log(logger, logging.INFO, "browser session opened", bill_to=config.bill_to,
                job_count=len(jobs), batch_size=spec.site.batch_size)
            await self._open_and_login(session, jobs[0], spec)
            site = spec.site
            log(logger, logging.INFO, "tracking page ready",
                page_url=_safe_url(session.current_url() or "", secrets), job_count=len(jobs))
            pattern = re.compile(site.response_url_pattern or site.response_regex or ".*")
            for chunk_index, chunk in enumerate(_chunks(jobs, site.batch_size)):
                if chunk_index:
                    if site.inter_batch_delay_s > 0:
                        await asyncio.sleep(site.inter_batch_delay_s)
                    else:
                        await self._delay(site.action_min_delay_s, site.action_max_delay_s)
                keys = {normalize_pro(job.search_value, spec): job.search_value for job in chunk}
                pros = [job.search_value for job in chunk]
                entered = [format_pro_for_input(p, spec) for p in pros]
                log(logger, logging.INFO, "tracking batch submit", chunk=chunk_index + 1,
                    pro_count=len(pros), pros=pros, entered_pros=entered)
                try:
                    await self._submit(session, chunk, spec, pattern, keys, result.received,
                                       result.outcomes, result.summaries)
                except Exception as exc:
                    if await session.looks_blocked(spec.block_markers or None):
                        exc = ChallengeError("carrier challenge detected")
                    log(logger, logging.WARNING, "tracking batch failed", stage="chunk",
                        error=type(exc).__name__, detail=self._safe_error(exc, secrets))
                    log(logger, logging.INFO, "scrape failure details", stage="chunk",
                        error=type(exc).__name__, detail=self._safe_error(exc, secrets),
                        pro_count=len(chunk))
                    result.errors.append({"type": type(exc).__name__, "pros": ",".join(
                        job.search_value for job in chunk), "message": self._safe_error(exc, secrets)})
                    await self._diagnose(session, spec, "chunk", secrets)
                finally:
                    await self._return_to_tracking(session, jobs[0], spec)
                retry_missing = (site.extra or {}).get("retry_missing_individually", True)
                if retry_missing:
                    received = {normalize_pro(key, spec) for key in result.received}
                    for job in chunk:
                        if normalize_pro(job.search_value, spec) in received:
                            continue
                        await self._delay(site.action_min_delay_s, site.action_max_delay_s)
                        try:
                            key = normalize_pro(job.search_value, spec)
                            await self._submit(session, [job], spec, pattern, {key: job.search_value},
                                               result.received, result.outcomes, result.summaries)
                        except Exception as exc:
                            if await session.looks_blocked(spec.block_markers or None):
                                exc = ChallengeError("carrier challenge detected")
                            log(logger, logging.WARNING, "tracking batch failed", stage="fallback",
                                pro=job.search_value, error=type(exc).__name__,
                                detail=self._safe_error(exc, secrets))
                            log(logger, logging.INFO, "scrape failure details", stage="fallback",
                                error=type(exc).__name__, detail=self._safe_error(exc, secrets))
                            result.errors.append({"type": type(exc).__name__, "pro": job.search_value,
                                                  "message": self._safe_error(exc, secrets)})
                            await self._diagnose(session, spec, "fallback", secrets)
                        finally:
                            await self._return_to_tracking(session, jobs[0], spec)
                else:
                    chunk_received = sum(1 for job in chunk
                                         if job.search_value in result.received)
                    log(logger, logging.INFO, "batch scrape complete (no per-PRO retry)",
                        chunk=chunk_index + 1, received=chunk_received, requested=len(chunk))
        except Exception as exc:
            if session and await session.looks_blocked(spec.block_markers or None):
                exc = ChallengeError("carrier challenge detected")
            safe_msg = self._safe_error(exc, secrets)
            log(logger, logging.ERROR, "batch scrape setup failed",
                client=spec.client, carrier_code=spec.code,
                error=type(exc).__name__, message=safe_msg)
            log(logger, logging.INFO, "scrape failure details", stage="setup",
                error=type(exc).__name__, detail=safe_msg)
            logger.exception("batch scrape setup failed for %s/%s: %s", spec.client, spec.code, safe_msg)
            result.errors.append({
                "type": type(exc).__name__,
                "pros": ",".join(result.requested),
                "message": safe_msg,
            })
            if session:
                await self._clear_credentials(session, spec)
                await self._diagnose(session, spec, "setup", secrets)
        finally:
            if session:
                try:
                    await session.close()
                except Exception:
                    pass
        received = {normalize_pro(key, spec) for key in result.received}
        result.missing = [value for value in result.requested if normalize_pro(value, spec) not in received]
        found_n = sum(1 for o in result.outcomes.values() if o == "found")
        not_found_n = sum(1 for o in result.outcomes.values() if o == "not_found")
        log(logger, logging.INFO, "batch scrape summary",
            captured=len(result.received), found=found_n, not_found=not_found_n,
            missing=len(result.missing), errors=len(result.errors))
        return result

    async def _fill_tracking_input(self, session: BrowserSession, site, chunk: list[TrackingJob],
                                   spec: CarrierSpec, input_selector: str | None = None) -> str:
        selector = input_selector or site.input_selector
        pros = [format_pro_for_input(job.search_value, spec) for job in chunk]
        separator = "\n" if site.input_mode == "multiline" else site.delimiter
        expected_text = separator.join(pros)
        extra = site.extra or {}
        if extra.get("tag_input_enter"):
            for index, pro in enumerate(pros):
                await session.fill(selector, pro, human=False)
                await session.press(selector, "Enter")
                if index + 1 < len(pros) and site.inter_pro_delay_s > 0:
                    await asyncio.sleep(site.inter_pro_delay_s)
            return expected_text
        gradual = site.typing_delay_ms > 0 or site.inter_pro_delay_s > 0

        if gradual:
            await session.type_text(selector, "", clear=True)
            for index, pro in enumerate(pros):
                if index > 0:
                    await session.type_text(selector, separator, delay_ms=0, clear=False)
                    if site.inter_pro_delay_s > 0:
                        await asyncio.sleep(site.inter_pro_delay_s)
                await session.type_text(selector, pro, delay_ms=site.typing_delay_ms, clear=False)
            return expected_text

        setter = getattr(session, "set_input_value", None)
        if setter is not None:
            await setter(selector, expected_text)
        else:
            await session.fill(selector, expected_text, human=False)
        return expected_text

    async def _verify_tracking_input(
        self,
        session: BrowserSession,
        selector: str,
        expected_text: str,
        chunk: list[TrackingJob],
    ) -> None:
        actual_val = await session.get_value(selector)
        expected_clean = _normalize_tracking_text(expected_text)
        actual_clean = _normalize_tracking_text(actual_val or "")
        if actual_clean == expected_clean:
            log(logger, logging.INFO, "tracking input verified",
                line_count=len(_tracking_lines(actual_clean)), mode="exact")
            return

        expected_lines = _tracking_lines(expected_clean)
        actual_lines = _tracking_lines(actual_clean)
        log(logger, logging.WARNING, "tracking input mismatch after entry",
            expected_lines=expected_lines, actual_lines=actual_lines,
            expected_line_count=len(expected_lines), actual_line_count=len(actual_lines))

        setter = getattr(session, "set_input_value", None)
        if setter is not None:
            await setter(selector, expected_text)
            actual_val = await session.get_value(selector)
            actual_clean = _normalize_tracking_text(actual_val or "")
            if actual_clean == expected_clean:
                log(logger, logging.INFO, "tracking input corrected via set_input_value",
                    line_count=len(_tracking_lines(actual_clean)))
                return

        merged = [line for line in actual_lines if len(_pro_digits(line)) > 12]
        if merged or (len(actual_lines) < len(expected_lines) and len(actual_lines) <= 2):
            raise ValueError(
                "PRO numbers were merged or truncated in the tracking box "
                f"(expected {len(expected_lines)} lines, got {len(actual_lines)}; merged={merged[:2]})"
            )
        if len(actual_lines) == 1 and len(expected_lines) > 1:
            raise ValueError(
                "PRO numbers were merged onto one line; multiline entry failed "
                f"(expected {len(expected_lines)} lines, got 1)"
            )

    async def _open_and_login(self, session: BrowserSession, job: TrackingJob, spec: CarrierSpec) -> None:
        site, config = spec.site, job.carrier
        tracking_url = site.tracking_url or config.tracking_url
        if not tracking_url:
            raise ValueError("carrier tracking URL is missing")
        await session.goto(tracking_url)
        if await session.looks_blocked(spec.block_markers or None):
            raise ChallengeError("carrier challenge detected")
        await self._dismiss_consent(session, spec)
        await execute_login(session, spec, config.user_name, config.password, tracking_url)
        settle = float((spec.site.extra or {}).get("landing_settle_s", 2))
        if settle > 0:
            await asyncio.sleep(settle)

    async def _return_to_tracking(self, session: BrowserSession, job: TrackingJob, spec: CarrierSpec) -> None:
        if not (spec.site.extra or {}).get("return_to_tracking_after_submit", True):
            return
        url = spec.site.tracking_url or job.carrier.tracking_url
        if not url:
            return
        try:
            current = (session.current_url() or "").split("?")[0].rstrip("/")
            target = url.split("?")[0].rstrip("/")
            if current != target:
                await session.goto(url)
                await self._dismiss_consent(session, spec)
                settle = float((spec.site.extra or {}).get("landing_settle_s", 1))
                if settle > 0:
                    await asyncio.sleep(settle)
        except Exception as exc:
            logger.warning("return to tracking page failed: %s", type(exc).__name__)

    async def _dismiss_consent(self, session: BrowserSession, spec: CarrierSpec) -> None:
        sels = (spec.site.extra or {}).get("consent_selectors") or DEFAULT_CONSENT_SELECTORS
        for sel in sels:
            try:
                if await session.is_visible(sel):
                    await session.click(sel)
                    log(logger, logging.INFO, "cookie banner dismissed", selector=sel)
                    await asyncio.sleep(1)
                    return
            except Exception:
                continue

    async def _resolve_tracking_selectors(self, session: BrowserSession, spec: CarrierSpec) -> tuple[str, str]:
        site = spec.site
        if not site.input_selector or not site.submit_selector:
            raise ValueError("input_selector and submit_selector must be configured")
        extra = site.extra or {}
        input_candidates = [site.input_selector, *(extra.get("input_selector_fallbacks") or [])]
        input_sel = ""
        for candidate in input_candidates:
            if not candidate:
                continue
            if await session.wait_for_selector(candidate, timeout_s=12):
                input_sel = candidate
                break
        submit_sel = site.submit_selector
        if not input_sel:
            discovered = await session.discover_search_box()
            if discovered and discovered.get("input"):
                input_sel = discovered["input"]
                submit_sel = site.submit_selector or discovered.get("submit") or submit_sel
                log(logger, logging.INFO, "tracking controls auto-discovered",
                    input_selector=input_sel, submit_selector=submit_sel)
        if not input_sel:
            raise TimeoutError("tracking input was not available")
        if submit_sel and not await session.wait_for_selector(submit_sel, timeout_s=8):
            discovered = await session.discover_search_box()
            if discovered and discovered.get("submit"):
                submit_sel = discovered["submit"]
                log(logger, logging.INFO, "submit control auto-discovered", submit_selector=submit_sel)
        log(logger, logging.INFO, "tracking controls ready", input_selector=input_sel, submit_selector=submit_sel)
        return input_sel, submit_sel

    def _record_pro_response(
        self,
        pro: str,
        raw: Any,
        spec: CarrierSpec,
        received: dict[str, Any],
        outcomes: dict[str, CaptureOutcome],
        summaries: dict[str, dict[str, Any]],
    ) -> None:
        outcome, summary = classify_carrier_raw(raw, spec)
        received[pro] = raw
        outcomes[pro] = outcome
        summaries[pro] = summary
        log(logger, logging.INFO, "tracking response recorded",
            pro=pro, outcome=outcome, error_code=summary.get("error_code"),
            error_message=summary.get("error_message"), data_count=summary.get("data_count"))
        if raw:
            try:
                pretty = json.dumps(raw, indent=2, default=str)
                logger.info("extracted JSON [pro=%s outcome=%s]:\n%s", pro, outcome, pretty)
            except Exception:
                pass

    async def _submit(self, session: BrowserSession, chunk: list[TrackingJob], spec: CarrierSpec,
                      pattern: re.Pattern, keys: dict[str, str], received: dict[str, Any],
                      outcomes: dict[str, CaptureOutcome],
                      summaries: dict[str, dict[str, Any]]) -> None:
        site = spec.site
        if await session.looks_blocked(spec.block_markers or None):
            raise ChallengeError("carrier challenge detected")
        input_sel, submit_sel = await self._resolve_tracking_selectors(session, spec)
        expected_text = await self._fill_tracking_input(session, site, chunk, spec, input_sel)
        if not (site.extra or {}).get("skip_tracking_input_verify"):
            await self._verify_tracking_input(session, input_sel, expected_text, chunk)

        wait_ready = getattr(session, "wait_ready", None)
        if wait_ready is not None and submit_sel:
            try:
                await wait_ready(submit_sel, timeout_s=min(20, site.response_timeout_s))
            except Exception as exc:
                logger.warning("submit control not ready before click: %s", type(exc).__name__)
        if site.pre_submit_delay_s > 0:
            await asyncio.sleep(site.pre_submit_delay_s)
        else:
            await self._delay(site.action_min_delay_s, site.action_max_delay_s)
        await self._handle_recaptcha(session, spec)

        prefer_rx = re.compile(site.response_url_prefer) if site.response_url_prefer else None

        def matching(responses: list[CapturedResponse]) -> list[CapturedResponse]:
            return [response for response in responses if pattern.search(response.url)]

        def is_preferred(response: CapturedResponse) -> bool:
            return bool(prefer_rx and prefer_rx.search(response.url))

        batch_collect = bool((site.extra or {}).get("batch_collect_all_responses"))

        def complete(responses: list[CapturedResponse]) -> bool:
            matches = matching(responses)
            if site.expected_response_shape == "single_batch":
                if not matches:
                    return False
                # Keep listening until the preferred (full-details) API arrives; capture_all
                # still falls back to quiet/timeout so find-by-pro is used if details never fire.
                if prefer_rx and not any(is_preferred(response) for response in matches):
                    return False
                return True
            if batch_collect:
                return False
            found = set()
            for response in matches:
                found.update(self._pro_keys_from_response(response, spec, keys, site))
            return set(keys).issubset(found)

        submit_force = bool((site.extra or {}).get("submit_force_click"))

        async def _click_submit() -> None:
            click = session.click
            if submit_force:
                await click(submit_sel, force=True)
            else:
                await click(submit_sel)

        responses = await session.capture_all(_click_submit,
            timeout_s=site.response_timeout_s, settle_s=site.idle_window_s,
            until=complete)
        matches = matching(responses)
        if prefer_rx:
            # Summary APIs first, preferred/full-details last so they overwrite per PRO
            # without the details response marking the rest of the chunk as not_found.
            matches = sorted(matches, key=lambda response: 1 if is_preferred(response) else 0)
        if not matches and responses and (site.extra or {}).get("response_match_loose_json"):
            for response in responses:
                if self._pro_keys_from_response(response, spec, keys, site):
                    matches.append(response)
                elif isinstance(response.json, dict) and response.json.get("data") is not None:
                    matches.append(response)
                elif isinstance(response.json, list):
                    matches.append(response)
        if await session.looks_blocked(spec.block_markers or None):
            raise ChallengeError("carrier challenge detected")
        if not matches:
            not_found = await self._carrier_not_found_message(session, spec)
            if not_found:
                log(logger, logging.WARNING, "carrier site reports tracking not found",
                    pros=list(keys.values()), detail=not_found[:200])
                return
            log(logger, logging.WARNING, "no tracking JSON matched pattern",
                pattern=site.response_url_pattern or site.response_regex,
                captured_json_urls=[_response_url_without_query(response.url) for response in responses[:12]],
                captured_count=len(responses))
            log(logger, logging.INFO, "tracking response match diagnostics",
                configured_pattern=site.response_url_pattern or site.response_regex,
                response_count=len(responses), matched_count=len(matches),
                sample_urls=[_response_url_without_query(response.url) for response in responses[:8]])
            raise TimeoutError("no tracking JSON response matched the configured URL pattern")
        log(logger, logging.INFO, "tracking API responses captured",
            response_count=len(matches),
            urls=[_response_url_without_query(response.url) for response in matches[:3]])
        for response in matches:
            if site.expected_response_shape == "single_batch":
                if self._assign_single_batch_response(
                    response, spec, keys, site, received, outcomes, summaries,
                    allow_unmatched_not_found=not is_preferred(response)):
                    continue
            found = self._pro_keys_from_response(response, spec, keys, site)
            if site.expected_response_shape == "single_batch" and not found:
                found = set(keys) if len(keys) == 1 else set()
            for normalized in found:
                self._record_pro_response(keys[normalized], response.json, spec,
                                          received, outcomes, summaries)
        await self._collect_full_details(session, spec, keys, received, outcomes, summaries)

    async def _collect_full_details(self, session: BrowserSession, spec: CarrierSpec,
                                    keys: dict[str, str], received: dict[str, Any],
                                    outcomes: dict[str, CaptureOutcome],
                                    summaries: dict[str, dict[str, Any]]) -> None:
        """Open each found PRO's detail view and capture its full-details API response.

        Enabled by site.extra.detail_response_pattern. The row to click comes from
        site.extra.detail_click_selector ({pro} = digits as entered; {pro_rx} = regex that
        tolerates separators between digits). The detail view is closed with
        site.extra.detail_back_selector, or browser history back."""
        site = spec.site
        extra = site.extra or {}
        pattern_text = extra.get("detail_response_pattern")
        if not pattern_text:
            return
        rx = re.compile(pattern_text)
        template = extra.get("detail_click_selector") or ':text-matches("{pro_rx}") >> nth=0'
        settle = float(extra.get("detail_settle_s", 1.5))
        timeout = float(extra.get("detail_timeout_s", 20))
        for normalized, pro in keys.items():
            if outcomes.get(pro) != "found":
                continue
            digits = normalized if normalized.isdigit() else _pro_digits(normalized)
            pro_rx = r"\D*".join(digits)
            selector = template.replace("{pro_rx}", pro_rx.replace("\\", "\\\\")).replace("{pro}", digits)
            try:
                async def _open() -> None:
                    await session.click(selector)
                got = await session.capture_all(
                    _open, timeout_s=timeout, settle_s=settle,
                    until=lambda rs: any(rx.search(r.url) and r.status == 200 for r in rs))
                details = [r for r in got if rx.search(r.url) and r.status == 200 and r.json]
                if not details:
                    log(logger, logging.WARNING, "no full-details response after click",
                        pro=pro, selector=selector)
                else:
                    one = {normalized: pro}
                    for response in details:
                        if not self._assign_single_batch_response(
                                response, spec, one, site, received, outcomes, summaries,
                                allow_unmatched_not_found=False):
                            self._record_pro_response(pro, response.json, spec, received, outcomes, summaries)
            except Exception as exc:
                log(logger, logging.WARNING, "full-details capture failed",
                    pro=pro, error=type(exc).__name__, detail=str(exc)[:200])
            back = extra.get("detail_back_selector")
            try:
                if back and await session.is_visible(back):
                    await session.click(back)
                else:
                    await session.evaluate("() => history.back()")
            except Exception:
                pass
            await asyncio.sleep(float(extra.get("detail_back_settle_s", 1.0)))

    def _single_batch_item_pro_path(self, site) -> str:
        extra = site.extra or {}
        if extra.get("single_batch_item_pro_path"):
            return extra["single_batch_item_pro_path"]
        pro_path = site.response_pro_path or ""
        if pro_path.startswith("[]"):
            return pro_path[2:].lstrip(".")
        if pro_path.startswith("[0]"):
            return pro_path[3:].lstrip(".")
        if "[]" in pro_path:
            return pro_path.split("[]", 1)[1].lstrip(".")
        return pro_path

    def _assign_single_batch_response(
        self,
        response: CapturedResponse,
        spec: CarrierSpec,
        keys: dict[str, str],
        site,
        received: dict[str, Any],
        outcomes: dict[str, CaptureOutcome],
        summaries: dict[str, dict[str, Any]],
        allow_unmatched_not_found: bool = True,
    ) -> bool:
        """Map one Saia-style batch API payload (top-level JSON array) onto each PRO in the chunk."""
        raw = response.json
        if isinstance(raw, dict):
            array_path = (site.extra or {}).get("single_batch_array_path")
            if array_path:
                values = _path_values(raw, array_path)
                if values and isinstance(values[0], list):
                    raw = values[0]
            elif "data" in raw and isinstance(raw["data"], list):
                raw = raw["data"]
            elif any(key in raw for key in (
                "referenceNumbers", "shipment", "proNumber", "shipmentInformation", "history",
            )):
                raw = [raw]
        if not isinstance(raw, list):
            return False
        item_pro_path = self._single_batch_item_pro_path(site)
        matched: set[str] = set()
        for item in raw:
            for value in _path_values_any(item, item_pro_path):
                normalized = normalize_pro(value, spec)
                if normalized not in keys:
                    continue
                matched.add(normalized)
                self._record_pro_response(keys[normalized], item, spec, received, outcomes, summaries)
        if matched:
            if allow_unmatched_not_found and (site.extra or {}).get("single_batch_unmatched_is_not_found"):
                unmatched = set(keys) - matched
                for normalized in unmatched:
                    pro = keys[normalized]
                    self._record_pro_response(pro, [], spec, received, outcomes, summaries)
                    outcomes[pro] = "not_found"
                    summaries[pro] = {"error_message": "shipment not found in carrier batch response", "data_count": 0}
            return True
        if not raw:
            for normalized in keys:
                self._record_pro_response(keys[normalized], raw, spec, received, outcomes, summaries)
            log(logger, logging.WARNING, "single batch API returned empty list",
                pros=list(keys.values()), chunk_size=len(keys))
            return True
        log(logger, logging.WARNING, "single batch API had no PRO keys matching this chunk",
            item_count=len(raw), pros=list(keys.values())[:5])
        return False

    def _pro_keys_from_response(
        self,
        response: CapturedResponse,
        spec: CarrierSpec,
        keys: dict[str, str],
        site,
    ) -> set[str]:
        found = self._matched_keys(response.json, site.response_pro_path, keys, spec)
        if found:
            return found
        query = parse_qs(urlsplit(response.url).query)
        for param in ("pro", "proNumber", "pronumber"):
            for raw in query.get(param, []):
                normalized = normalize_pro(raw, spec)
                if normalized in keys:
                    found.add(normalized)
        return found

    def _matched_keys(self, raw: Any, path: str, keys: dict[str, str], spec: CarrierSpec) -> set[str]:
        return {normalized for value in _path_values_any(raw, path)
                if (normalized := normalize_pro(value, spec)) in keys}

    async def _carrier_not_found_message(self, session: BrowserSession, spec: CarrierSpec) -> str | None:
        markers = (spec.site.extra or {}).get("not_found_markers") or [
            "not found or tracking information unavailable",
            "tracking information unavailable",
            "no shipments found",
        ]
        for marker in markers:
            try:
                found = await session.evaluate(
                    f"() => document.body && document.body.innerText.toLowerCase().includes({marker.lower()!r})"
                )
            except Exception:
                found = False
            if found:
                return marker
        return None

    async def _delay(self, minimum: float, maximum: float) -> None:
        if maximum > 0:
            await asyncio.sleep(random.uniform(max(0, minimum), max(minimum, maximum)))

    async def _clear_credentials(self, session: BrowserSession, spec: CarrierSpec) -> None:
        login = spec.site.login
        if not login:
            return
        selectors = login.get("selectors", login)
        for key in ("username", "password"):
            selector = selectors.get(key)
            if selector:
                try:
                    await session.fill(selector, "", human=False)
                except Exception:
                    pass

    async def _handle_recaptcha(self, session: BrowserSession, spec: CarrierSpec) -> None:
        anchor_frame = 'iframe[title="reCAPTCHA"]:visible'
        checkbox = "#recaptcha-anchor"
        checked_selector = "#recaptcha-anchor[aria-checked='true']"

        try:
            page = getattr(session, "page", None)
            if page is not None:
                has_recaptcha = await page.frame_locator(anchor_frame).locator(checkbox).is_visible()
            else:
                has_recaptcha = await session.is_visible(checkbox, frame=anchor_frame)
        except Exception:
            has_recaptcha = False

        if not has_recaptcha:
            return

        try:
            if page is not None:
                if await page.frame_locator(anchor_frame).locator(checked_selector).is_visible():
                    return
            elif await session.is_visible(checked_selector, frame=anchor_frame):
                return
        except Exception:
            pass

        logger.info("clicking reCAPTCHA checkbox for %s", spec.code)
        try:
            if page is not None:
                await page.frame_locator(anchor_frame).locator(checkbox).click(timeout=5000)
            else:
                await session.click(checkbox, frame=anchor_frame)
        except Exception as exc:
            logger.warning("failed to click reCAPTCHA checkbox: %s", exc)
            return

        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.4)
            try:
                if page is not None:
                    if await page.frame_locator(anchor_frame).locator(checked_selector).is_visible():
                        logger.info("reCAPTCHA verified successfully for %s", spec.code)
                        await asyncio.sleep(0.5)
                        return
                elif await session.is_visible(checked_selector, frame=anchor_frame):
                    logger.info("reCAPTCHA verified successfully for %s", spec.code)
                    await asyncio.sleep(0.5)
                    return

                challenge = bool(await session.evaluate("""() => {
                    const bframes = Array.from(document.querySelectorAll('iframe[src*="recaptcha/api2/bframe"], iframe[title*="recaptcha challenge"]'));
                    for (const bf of bframes) {
                        const rect = bf.getBoundingClientRect();
                        const style = window.getComputedStyle(bf);
                        if (rect.width > 200 && rect.height > 200 && rect.top > 0 && style.visibility === 'visible' && style.display !== 'none') {
                            return true;
                        }
                    }
                    return false;
                }"""))
                if challenge:
                    raise ChallengeError("reCAPTCHA image challenge presented (manual intervention required)")
            except ChallengeError:
                raise
            except Exception:
                pass

    @staticmethod
    def _safe_error(exc: Exception, secrets: list[str]) -> str:
        message = str(exc)
        for secret in secrets:
            message = message.replace(secret, "REDACTED")
        return message[:300]

    async def _diagnose(self, session: BrowserSession, spec: CarrierSpec, stage: str,
                        secrets: list[str]) -> None:
        self._diagnostic_index += 1
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{spec.client}_{spec.code}_{stage}_{self._diagnostic_index}")
        screenshot = self.debug_dir / f"{name}.png"
        url_file = self.debug_dir / f"{name}.url"
        try:
            path = await session.save_debug(str(screenshot))
            url_file.parent.mkdir(parents=True, exist_ok=True)
            url_file.write_text(_safe_url(session.current_url(), secrets), encoding="utf-8")
            logger.warning("scrape diagnostic saved", extra={"fields": {"client": spec.client,
                "carrier_code": spec.code, "screenshot": path, "page_url_file": str(url_file)}})
        except Exception as exc:
            logger.warning("scrape diagnostic could not be saved", extra={"fields": {
                "carrier_code": spec.code, "error": type(exc).__name__}})
