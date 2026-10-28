"""Browser Use integration dedicated exclusively to solving CAPTCHA challenges on the active Playwright page."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from app.browser.base import BrowserSession
from app.settings import CarrierSpec

logger = logging.getLogger(__name__)


async def solve_captcha_with_browser_use(
    session: BrowserSession,
    spec: CarrierSpec,
    timeout_s: float = 60.0,
) -> bool:
    """Delegate Google reCAPTCHA solving to Browser Use on the active Playwright browser session via CDP.

    Returns True if the challenge was verified and cleared, False otherwise.
    """
    cdp_url = getattr(session, "cdp_url", None) or os.environ.get("CDP_URL") or "http://127.0.0.1:9222"

    # Check for LLM API keys
    has_llm_key = any(
        os.environ.get(k)
        for k in (
            "ANTHROPIC_API_KEY",
            "CLAUDE_API_KEY",
        )
    )
    if not has_llm_key:
        logger.info(
            "Browser Use CAPTCHA solver: No LLM API key detected in environment. "
            "Set OPENAI_API_KEY or BROWSER_USE_API_KEY to enable automated Browser Use CAPTCHA solving. "
            "Falling back to human-assisted verification."
        )
        return False

    try:
        from browser_use import Agent
        from browser_use.browser.session import BrowserSession as BUBrowserSession
    except ImportError as e:
        logger.warning("Browser Use is not installed or import failed: %s", e)
        return False

    logger.info("Connecting Browser Use to active Playwright browser via CDP: %s", cdp_url)
    bu_session: BUBrowserSession | None = None
    try:
        bu_session = BUBrowserSession(cdp_url=cdp_url)
        await asyncio.wait_for(bu_session.connect(), timeout=10.0)

        task = (
            "A Google reCAPTCHA challenge is displayed on the page. "
            "Examine the instruction header at the top of the challenge (such as 'select all images with a bus', 'crosswalks', 'traffic lights', or 'motorcycles'). "
            "Carefully click all matching image tiles in the grid. "
            "Click the 'Verify' button. "
            "If another challenge appears, repeat until the challenge popup closes and the reCAPTCHA checkbox is checked."
        )

        agent = Agent(
            task=task,
            browser_session=bu_session,
            use_vision=True,
            max_failures=3,
            step_timeout=int(timeout_s),
        )

        logger.info("Executing Browser Use CAPTCHA solving agent...")
        await asyncio.wait_for(agent.run(), timeout=timeout_s)

        # Check if the challenge is solved on the Playwright session
        is_verified = False
        token = await session.evaluate("""() => {
            const el = document.querySelector('#g-recaptcha-response') || document.querySelector('#g-recaptcha-response-1');
            return el && el.value ? el.value : '';
        }""")
        if token:
            is_verified = True

        if not is_verified:
            is_verified = bool(await session.evaluate("""() => {
                const bframes = Array.from(document.querySelectorAll('iframe[src*="recaptcha/api2/bframe"], iframe[title*="recaptcha challenge"]'));
                for (const bf of bframes) {
                    const rect = bf.getBoundingClientRect();
                    const style = window.getComputedStyle(bf);
                    if (rect.width > 200 && rect.height > 200 && rect.top > 0 && style.visibility === 'visible' && style.display !== 'none') {
                        return false;
                    }
                }
                const checked = document.querySelector('.recaptcha-checkbox[aria-checked="true"]');
                return Boolean(checked);
            }"""))

        if is_verified:
            logger.info("Browser Use successfully verified the reCAPTCHA challenge!")
            return True
        else:
            logger.warning("Browser Use completed agent run, but reCAPTCHA challenge is still pending.")
            return False

    except Exception as exc:
        logger.warning("Browser Use CAPTCHA solver encountered an issue: %s (%s)", type(exc).__name__, exc)
        return False
    finally:
        if bu_session is not None:
            try:
                await bu_session.close()
            except Exception:
                pass
