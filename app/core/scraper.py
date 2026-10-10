"""Network-response batch scraper driven entirely by carrier YAML configuration."""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.browser.base import BrowserProvider, BrowserSession, CapturedResponse
from app.core.adapter import session_key
from app.core.login import execute_login
from app.models import TrackingJob
from app.settings import CarrierSpec

logger = logging.getLogger(__name__)


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


class Scraper(Protocol):
    async def run(self, job: TrackingJob) -> ScrapeBatchResult: ...


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


def normalize_pro(value: Any, spec: CarrierSpec) -> str:
    result = str(value or "").strip()
    if spec.site.strip_spaces_dashes:
        result = re.sub(r"[\s-]+", "", result)
    if spec.site.strip_leading_zeros:
        result = result.lstrip("0") or "0"
    return result.casefold()


def _chunks(items: list[TrackingJob], size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


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
            session = await self.provider.open_session(session_key(spec))
            await self._open_and_login(session, jobs[0], spec)
            site = spec.site
            pattern = re.compile(site.response_url_pattern or site.response_regex or ".*")
            for chunk_index, chunk in enumerate(_chunks(jobs, site.batch_size)):
                if chunk_index:
                    if site.inter_batch_delay_s > 0:
                        await asyncio.sleep(site.inter_batch_delay_s)
                    else:
                        await self._delay(site.action_min_delay_s, site.action_max_delay_s)
                keys = {normalize_pro(job.search_value, spec): job.search_value for job in chunk}
                try:
                    await self._submit(session, chunk, spec, pattern, keys, result.received)
                except Exception as exc:
                    if await session.looks_blocked(spec.block_markers or None):
                        exc = ChallengeError("carrier challenge detected")
                    result.errors.append({"type": type(exc).__name__, "pros": ",".join(
                        job.search_value for job in chunk), "message": self._safe_error(exc, secrets)})
                    await self._diagnose(session, spec, "chunk", secrets)
                received = {normalize_pro(key, spec) for key in result.received}
                for job in chunk:
                    if normalize_pro(job.search_value, spec) in received:
                        continue
                    await self._delay(site.action_min_delay_s, site.action_max_delay_s)
                    try:
                        key = normalize_pro(job.search_value, spec)
                        await self._submit(session, [job], spec, pattern, {key: job.search_value}, result.received)
                    except Exception as exc:
                        if await session.looks_blocked(spec.block_markers or None):
                            exc = ChallengeError("carrier challenge detected")
                        result.errors.append({"type": type(exc).__name__, "pro": job.search_value,
                                              "message": self._safe_error(exc, secrets)})
                        await self._diagnose(session, spec, "fallback", secrets)
        except Exception as exc:
            if session and await session.looks_blocked(spec.block_markers or None):
                exc = ChallengeError("carrier challenge detected")
            result.errors.append({"type": type(exc).__name__, "message": self._safe_error(exc, secrets)})
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
        return result

    async def _open_and_login(self, session: BrowserSession, job: TrackingJob, spec: CarrierSpec) -> None:
        site, config = spec.site, job.carrier
        tracking_url = site.tracking_url or config.tracking_url
        if not tracking_url:
            raise ValueError("carrier tracking URL is missing")
        await session.goto(tracking_url)
        if await session.looks_blocked(spec.block_markers or None):
            raise ChallengeError("carrier challenge detected")
        await execute_login(session, spec, config.user_name, config.password, tracking_url)

    async def _submit(self, session: BrowserSession, chunk: list[TrackingJob], spec: CarrierSpec,
                      pattern: re.Pattern, keys: dict[str, str], received: dict[str, Any]) -> None:
        site = spec.site
        if not site.input_selector or not site.submit_selector:
            raise ValueError("input_selector and submit_selector must be configured")
        if await session.looks_blocked(spec.block_markers or None):
            raise ChallengeError("carrier challenge detected")
        if not await session.wait_for_selector(site.input_selector, timeout_s=site.response_timeout_s):
            raise TimeoutError("tracking input was not available")
        separator = "\n" if site.input_mode == "multiline" else site.delimiter
        expected_text = separator.join(job.search_value for job in chunk)

        if site.typing_delay_ms > 0 or site.inter_pro_delay_s > 0:
            await session.type_text(site.input_selector, "", clear=True)
            for i, job in enumerate(chunk):
                if i > 0:
                    await session.type_text(site.input_selector, separator, delay_ms=0, clear=False)
                    if site.inter_pro_delay_s > 0:
                        await asyncio.sleep(site.inter_pro_delay_s)
                await session.type_text(site.input_selector, job.search_value, delay_ms=site.typing_delay_ms, clear=False)
        else:
            await session.fill(site.input_selector, expected_text, human=False)

        actual_val = await session.get_value(site.input_selector)
        if actual_val:
            actual_clean = actual_val.replace("\r\n", "\n").strip()
            expected_clean = expected_text.strip()
            if actual_clean != expected_clean:
                logger.warning(
                    "tracking input verification mismatch: expected %d chars, got %d chars",
                    len(expected_clean), len(actual_clean)
                )

        if site.pre_submit_delay_s > 0:
            await asyncio.sleep(site.pre_submit_delay_s)
        else:
            await self._delay(site.action_min_delay_s, site.action_max_delay_s)
        await self._handle_recaptcha(session, spec)

        def matching(responses: list[CapturedResponse]) -> list[CapturedResponse]:
            return [response for response in responses if pattern.search(response.url)]

        def complete(responses: list[CapturedResponse]) -> bool:
            if site.expected_response_shape == "single_batch":
                return bool(matching(responses))
            found = set()
            for response in matching(responses):
                found.update(self._matched_keys(response.json, site.response_pro_path, keys, spec))
            return set(keys).issubset(found)

        responses = await session.capture_all(lambda: session.click(site.submit_selector),
            timeout_s=site.response_timeout_s, settle_s=site.idle_window_s,
            until=complete)
        matches = matching(responses)
        if await session.looks_blocked(spec.block_markers or None):
            raise ChallengeError("carrier challenge detected")
        if not matches:
            raise TimeoutError("no tracking JSON response matched the configured URL pattern")
        for response in matches:
            found = self._matched_keys(response.json, site.response_pro_path, keys, spec)
            if site.expected_response_shape == "single_batch" and not found:
                found = set(keys) if len(keys) == 1 else set()
            for normalized in found:
                received[keys[normalized]] = response.json

    def _matched_keys(self, raw: Any, path: str, keys: dict[str, str], spec: CarrierSpec) -> set[str]:
        return {normalized for value in _path_values(raw, path)
                if (normalized := normalize_pro(value, spec)) in keys}

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
