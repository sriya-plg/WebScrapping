"""Unified carrier portal login orchestration.

Single source of truth for both BatchScraper and CarrierAdapter subclasses.
Handles modal and page-based login, credentials verification, MFA detection,
login failure detection, session state persistence, and post-login tracking verification.
Never logs credentials or secret values.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.core.resilience import ChallengeError, ManualInterventionRequired

if TYPE_CHECKING:
    from app.browser.base import BrowserSession
    from app.settings import CarrierSpec

logger = logging.getLogger(__name__)


class LoginError(RuntimeError):
    """Base error for carrier login issues."""


class LoginCredentialsMissingError(LoginError):
    """Login is required but username or password was not provided."""


class LoginFailedError(LoginError):
    """Authentication failed due to invalid credentials, rejected submission, or timeout."""


class MFAChallengeError(LoginError):
    """Two-factor authentication or one-time code challenge presented."""


class SessionExpiredError(LoginError):
    """Session expired or tracking page could not be accessed after login."""


DEFAULT_MFA_MARKERS = [
    "verification code",
    "two-factor",
    "one-time password",
    "mfa",
    "security code",
    "authenticator code",
    "enter code",
]

DEFAULT_FAILURE_MARKERS = [
    "invalid username",
    "invalid password",
    "invalid credentials",
    "login failed",
    "incorrect username",
    "incorrect password",
    "authentication failed",
    "account locked",
]


async def execute_login(
    session: BrowserSession,
    spec: CarrierSpec,
    username: str | None,
    password: Any | None,
    tracking_url: str | None = None,
) -> None:
    """Execute authenticated carrier login using the current browser session.

    Disabled by default unless site.login.enabled is explicitly True.
    Preserves cookies/session via session.save_state().
    """
    site = spec.site
    login = site.login
    if not login or not login.get("enabled", False):
        return

    pwd_val = password.get_secret_value() if hasattr(password, "get_secret_value") else (password or "")
    user_val = username or ""
    if not user_val or not pwd_val:
        if login.get("required", False):
            raise LoginCredentialsMissingError(
                f"Login is required for carrier {spec.code} but credentials are not configured"
            )
        logger.info("Login enabled for %s but credentials missing; proceeding with public tracking", spec.code)
        return

    login_url = login.get("url") or tracking_url or site.tracking_url
    if not login_url:
        raise LoginError("Login URL is not configured")

    current = session.current_url() if hasattr(session, "current_url") else ""
    if login.get("url") and current != login["url"]:
        await session.goto(login["url"])

    if await session.looks_blocked(spec.block_markers or None):
        raise ChallengeError(f"Bot challenge detected before login for {spec.code}")

    selectors = login.get("selectors", login)
    user_sel = selectors.get("username") or selectors.get("user_selector")
    pass_sel = selectors.get("password") or selectors.get("pass_selector")
    submit_sel = selectors.get("submit") or selectors.get("submit_selector")
    success_sel = selectors.get("success") or selectors.get("logged_in_selector")
    open_sel = login.get("open_selector") or selectors.get("open_selector")

    # If login modal trigger is needed (e.g. Saia desktop header button)
    if open_sel:
        try:
            if await session.is_visible(open_sel):
                await session.click(open_sel)
                await asyncio.sleep(0.5)
        except Exception as exc:
            logger.debug("modal opener click bypassed: %s", exc)

    if not user_sel or not pass_sel or not submit_sel:
        raise LoginError(f"Carrier {spec.code} login selectors (username, password, submit) are incomplete")

    if not await session.wait_for_selector(user_sel, timeout_s=min(20, site.response_timeout_s)):
        if await session.looks_blocked(spec.block_markers or None):
            raise ChallengeError(f"Bot challenge detected on login page for {spec.code}")
        raise LoginFailedError(f"Login input field ({user_sel}) not found for {spec.code}")

    # Enter credentials without logging them
    await session.fill(user_sel, user_val, human=False)
    await session.fill(pass_sel, pwd_val, human=False)
    await session.click(submit_sel)

    # Detect outcome
    mfa_markers = login.get("mfa_markers", DEFAULT_MFA_MARKERS)
    failure_markers = login.get("failure_markers", DEFAULT_FAILURE_MARKERS)
    timeout_s = float(login.get("timeout_s", min(25, site.response_timeout_s)))
    deadline = asyncio.get_running_loop().time() + timeout_s

    has_verified_success = bool(success_sel and "TODO" not in success_sel.upper())
    login_confirmed = False

    while asyncio.get_running_loop().time() < deadline:
        if await session.looks_blocked(spec.block_markers or None):
            raise ChallengeError(f"Bot challenge detected during login for {spec.code}")

        # Check for failure error notices in DOM
        for fail_text in failure_markers:
            has_fail = await session.evaluate(
                f"() => document.body && document.body.innerText.toLowerCase().includes('{fail_text.lower()}')"
            )
            if has_fail:
                raise LoginFailedError(f"Authentication failed for {spec.code}: invalid credentials detected")

        # Check for MFA challenges in DOM
        for mfa_text in mfa_markers:
            has_mfa = await session.evaluate(
                f"() => document.body && document.body.innerText.toLowerCase().includes('{mfa_text.lower()}')"
            )
            if has_mfa:
                raise MFAChallengeError(f"MFA challenge presented for carrier {spec.code} (manual intervention required)")

        if has_verified_success:
            if await session.is_visible(success_sel):
                login_confirmed = True
                break
        else:
            # If success selector is unverified / TODO, check if username input is gone (modal closed or redirected)
            if not await session.is_visible(user_sel):
                login_confirmed = True
                break

        await asyncio.sleep(0.4)

    if has_verified_success and not login_confirmed:
        raise LoginFailedError(f"Carrier {spec.code} login did not complete within {timeout_s}s (success selector not found)")

    # Save session state (cookies, local storage)
    await session.save_state()

    # Navigate to tracking page and confirm tracking input is ready
    target_track_url = tracking_url or site.tracking_url
    if target_track_url:
        await session.goto(target_track_url)
        if await session.looks_blocked(spec.block_markers or None):
            raise ChallengeError(f"Bot challenge detected navigating to tracking page for {spec.code}")
        if site.input_selector:
            if not await session.wait_for_selector(site.input_selector, timeout_s=site.response_timeout_s):
                raise SessionExpiredError(
                    f"Tracking input ({site.input_selector}) not available on {target_track_url} after login"
                )
