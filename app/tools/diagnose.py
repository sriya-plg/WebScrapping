"""Standalone browser and environment diagnostic tool.

Runs lifecycle steps 1 to 7 against a real carrier configuration without intake, PRO entry,
or TMS updates.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from app.backend.client import make_backend
from app.browser.playwright_provider import PlaywrightProvider
from app.browser.session_utils import session_key
from app.core.diagnostics import (
    check_block_page,
    log_environment_report,
    save_failure_artifacts,
    setup_asyncio_exception_handler,
    traced_step,
)
from app.logging_setup import (
    ConsoleFormatter,
    FlushFileHandler,
    carrier_var,
    client_var,
    run_id_var,
)
from app.settings import CarrierSpec, load_resolver, load_settings

logger = logging.getLogger("app.tools.diagnose")


def setup_diagnose_logging(carrier: str, logs_dir: Path) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"diagnose_{carrier.lower()}_{ts}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(ConsoleFormatter())
    root.addHandler(ch)

    # File handler
    fh = FlushFileHandler(str(log_file), encoding="utf-8")
    fh.setFormatter(ConsoleFormatter())
    root.addHandler(fh)

    return log_file


async def run_diagnose(
    carrier_code: str,
    client_name: str | None = None,
    headed: bool = False,
    headless: bool = False,
    url_override: str | None = None,
    config_path: str | None = None,
) -> int:
    run_id = f"diag_{carrier_code.lower()}_{int(time.time())}"
    run_id_var.set(run_id)

    diag_dir = Path("app/runtime/diagnostics")
    log_file = setup_diagnose_logging(carrier_code, diag_dir)
    logger.info("diagnose tool started | carrier=%s log_file=%s", carrier_code, log_file)

    try:
        setup_asyncio_exception_handler(asyncio.get_running_loop(), logger)
    except Exception:
        pass

    settings = load_settings()
    resolver = load_resolver()
    backend = make_backend(settings.backend)

    # 1. Environment report
    log_environment_report(settings, resolver, logger)

    # 2. Resolve the built-in interaction profile and obtain the tracking URL from the backend.
    target_code = carrier_code.strip().upper()
    config = None
    spec: CarrierSpec | None = None
    try:
        configs = await backend.get_carrier_configs()
        for item in configs:
            candidate = resolver.resolve(str(item.bill_to), item.carrier_code, config=item)
            if not candidate:
                continue
            identifiers = {candidate.code.upper(), item.carrier_code.upper(),
                           *(alias.upper() for alias in candidate.aliases)}
            if target_code not in identifiers:
                continue
            if client_name and client_name.strip().casefold() not in {
                candidate.client.casefold(), str(item.bill_to).casefold(),
                (item.customer_name or "").casefold()
            }:
                continue
            config, spec = item, candidate
            break
    except Exception as exc:
        logger.error("could not load carrier configuration from backend: %s", type(exc).__name__)
        await backend.close()
        return 1

    if spec is None or config is None:
        logger.error("carrier '%s' was not found in backend configurations or has no built-in profile", carrier_code)
        await backend.close()
        return 1

    client_var.set(spec.client)
    carrier_var.set(spec.code)
    logger.info("resolved carrier profile | client=%s carrier=%s", spec.client, spec.code)

    if headed:
        os.environ["BROWSER_HEADLESS"] = "0"
        spec.browser.headless = False
    elif headless:
        os.environ["BROWSER_HEADLESS"] = "1"
        spec.browser.headless = True

    tracking_url = url_override or config.tracking_url
    if not tracking_url:
        logger.error("backend carrier configuration has no trackingUrl and no --url override provided")
        await backend.close()
        return 1

    provider = PlaywrightProvider(spec.browser, settings.browser_state_dir)
    step_results: list[dict[str, Any]] = []
    first_failure: dict[str, Any] | None = None
    session = None

    steps_plan = [
        (1, "start playwright driver"),
        (2, "launch browser"),
        (3, "create context"),
        (4, "apply stealth / patches"),
        (5, "new page"),
        (6, "attach listeners"),
        (7, "navigate to tracking URL"),
    ]

    try:
        # Steps 1 to 6 happen inside open_session
        t_open = time.monotonic()
        try:
            session = await provider.open_session(session_key(spec))
            elapsed_open = (time.monotonic() - t_open) * 1000
            # Record success for steps 1-6
            for s_num, s_name in steps_plan[:6]:
                step_results.append({"step": s_num, "name": s_name, "status": "PASS", "ms": elapsed_open / 6})
        except Exception as exc:
            elapsed_open = (time.monotonic() - t_open) * 1000
            failed_step_name = getattr(exc, "failed_step", "open_session")
            failed_num = getattr(exc, "failed_step_num", 2)
            first_failure = {
                "step": failed_num,
                "name": failed_step_name,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            }
            step_results.append({"step": failed_num, "name": failed_step_name, "status": "FAIL", "ms": elapsed_open})
            raise

        # Step 7: navigate to tracking URL
        t7 = time.monotonic()
        try:
            async with traced_step(7, "navigate to tracking URL", extra={"url": tracking_url}) as step7:
                resp = await session.goto(tracking_url)
                status_code = getattr(resp, "status", None) if resp else 200
                final_url = session.current_url()
                title = ""
                try:
                    title = await session.page.title() if hasattr(session, "page") else ""
                except Exception:
                    pass
                step7.set_info(http_status=status_code, final_url=final_url, title=title)

                page_text = ""
                try:
                    if hasattr(session, "page"):
                        page_text = (await session.page.inner_text("body"))[:2000]
                except Exception:
                    pass
                block_reason = check_block_page(page_text, final_url, status_code, title)
                if block_reason:
                    logger.warning("block page marker detected: %s (url=%s, title=%s)", block_reason, final_url, title)

            ms7 = (time.monotonic() - t7) * 1000
            step_results.append({"step": 7, "name": "navigate to tracking URL", "status": "PASS", "ms": ms7})
        except Exception as exc:
            ms7 = (time.monotonic() - t7) * 1000
            first_failure = {
                "step": 7,
                "name": "navigate to tracking URL",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            }
            step_results.append({"step": 7, "name": "navigate to tracking URL", "status": "FAIL", "ms": ms7})
            raise

        # Save artifacts
        if session and hasattr(session, "page"):
            artifacts = await save_failure_artifacts(
                session.page,
                getattr(session, "context", None),
                run_id=run_id,
                carrier_code=spec.code,
                step_name="step7_success",
                diagnostics_dir=diag_dir,
            )
            logger.info("diagnose artifacts saved: %s", artifacts)

    except Exception:
        if session and hasattr(session, "page"):
            try:
                failed_name = first_failure["name"] if first_failure else "step"
                await save_failure_artifacts(
                    session.page,
                    getattr(session, "context", None),
                    run_id=run_id,
                    carrier_code=spec.code,
                    step_name=failed_name,
                    diagnostics_dir=diag_dir,
                )
            except Exception:
                pass
    finally:
        try:
            async with traced_step(13, "close page/context/browser"):
                if session:
                    await session.close()
                await provider.close()
        except Exception as exc:
            logger.warning("error closing provider: %s", exc)
        await backend.close()

    # Print summary report
    print("\n================ DIAGNOSTIC STEP SUMMARY ================")
    for res in step_results:
        print(f"[STEP {res['step']}/7] {res['name']:<32} {res['status']} ({res['ms']:.1f} ms)")

    if first_failure:
        print("\n================ FIRST FAILURE TRACEBACK ================")
        print(f"Failed at [STEP {first_failure['step']}/7] {first_failure['name']}")
        print(f"Error: {first_failure['error']}\n")
        print(first_failure["traceback"])
        print("=========================================================\n")
        logger.error("diagnose finished with FAILURE at step %s", first_failure["name"])
        return 1

    print("\nALL LIFECYCLE STEPS 1-7 PASSED SUCCESSFULLY.")
    print("=========================================================\n")
    logger.info("diagnose finished successfully")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Freight scraper browser and environment diagnostic tool")
    parser.add_argument("--carrier", required=True, help="Carrier code (e.g. FORWARDAIR, SAIA, ESTES)")
    parser.add_argument("--client", default=None, help="Optional client key (e.g. calix)")
    parser.add_argument("--headed", action="store_true", help="Launch browser in headed mode")
    parser.add_argument("--headless", action="store_true", help="Launch browser in headless mode")
    parser.add_argument("--url", default=None, help="Override tracking URL for navigation")

    args = parser.parse_args()

    exit_code = asyncio.run(
        run_diagnose(
            carrier_code=args.carrier,
            client_name=args.client,
            headed=args.headed,
            headless=args.headless,
            url_override=args.url,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
