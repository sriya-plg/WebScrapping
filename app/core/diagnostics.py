"""Environment diagnostics, lifecycle tracing artifacts, and crash hooks."""
from __future__ import annotations

import asyncio
import getpass
import importlib.metadata
import importlib.util
import io
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.logging_setup import redact

logger = logging.getLogger(__name__)

# Common markers indicating bot challenges, WAF blocks, or access denial
BLOCK_PAGE_MARKERS = [
    "access denied",
    "captcha",
    "verify you are human",
    "cloudflare",
    "reference #",
    "request blocked",
    "attention required",
    "just a moment",
    "security check",
    "incident id",
    "ray id",
]


def mask_proxy_url(url: str | None) -> str:
    """Mask username/password credentials in proxy URLs."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if parts.username or parts.password:
            user = parts.username or ""
            netloc = f"{user}:***@{parts.hostname}"
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    # Regex fallback for user:pass@
    return re.sub(r"://([^:@]+):[^@]+@", r"://\1:***@", str(url))


def get_package_version(name: str) -> str:
    """Return installed package version or 'not installed'."""
    try:
        if importlib.util.find_spec(name) is None:
            return "not installed"
        return importlib.metadata.version(name)
    except Exception:
        return "not installed"


def get_chromium_executable_path() -> tuple[str, bool]:
    """Resolve chromium executable path using playwright and check existence."""
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    try:
        # Run in a separate thread so sync_playwright never conflicts with any running event loop
        def _resolve():
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                return p.chromium.executable_path

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_resolve)
            resolved = future.result(timeout=10)
            exists = os.path.exists(resolved) if resolved else False
            return str(resolved), exists
    except Exception as exc:
        return f"error resolving path ({type(exc).__name__}: {exc})", False


def check_directory_writable(dir_path: str | Path) -> tuple[bool, str]:
    """Test actual writability of a directory by writing and removing a temporary file."""
    p = Path(dir_path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        test_file = p / f".write_test_{int(time.time() * 1000)}.tmp"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return True, "writable"
    except Exception as exc:
        return False, f"not writable: {type(exc).__name__}: {exc}"


def get_free_disk_space_gb(path: str | Path) -> float | None:
    """Return free disk space in GB for the given path."""
    try:
        usage = shutil.disk_usage(str(path))
        return round(usage.free / (1024 ** 3), 2)
    except Exception:
        return None


def get_environment_report(settings: Any = None, resolver: Any = None) -> dict[str, Any]:
    """Generate structured dictionary containing full runtime environment details."""
    logs_dir = getattr(settings, "logs_dir", "logs") if settings else "logs"
    temp_dir = tempfile.gettempdir()

    logs_writable, logs_writable_msg = check_directory_writable(logs_dir)
    temp_writable, temp_writable_msg = check_directory_writable(temp_dir)
    logs_free_gb = get_free_disk_space_gb(logs_dir)

    chromium_path, chromium_exists = get_chromium_executable_path()

    # Event loop check
    loop_policy_name = type(asyncio.get_event_loop_policy()).__name__
    try:
        running_loop = asyncio.get_running_loop()
        loop_class = type(running_loop).__name__
    except RuntimeError:
        loop_class = "none (no running loop)"

    is_windows = sys.platform == "win32"
    proactor_class = getattr(asyncio, "ProactorEventLoop", None)
    is_proactor = False
    if is_windows:
        try:
            running_loop = asyncio.get_running_loop()
            is_proactor = proactor_class is not None and isinstance(running_loop, proactor_class)
        except Exception:
            is_proactor = False
        loop_status = f"{loop_class} (Proactor={is_proactor})"
        if loop_class != "none (no running loop)" and not is_proactor:
            loop_status += " [WARNING: SelectorEventLoop on Windows breaks Playwright subprocess spawning; ProactorEventLoop required]"
    else:
        loop_status = f"{loop_class} (policy={loop_policy_name})"

    # Effective carrier browser configurations
    carrier_configs: dict[str, Any] = {}
    if resolver is not None:
        try:
            for (client_key, carrier_key), spec in getattr(resolver, "_specs", {}).items():
                label = f"{client_key}/{carrier_key}"
                b = spec.browser
                headless = b.headless
                env_hl = os.environ.get("BROWSER_HEADLESS") or os.environ.get("HEADLESS")
                if env_hl is not None:
                    headless = env_hl.lower() in ("true", "1", "yes")
                elif os.name == "posix" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                    headless = True

                carrier_configs[label] = {
                    "headless": headless,
                    "configured_headless": b.headless,
                    "browser_mode": b.browser_mode or b.provider,
                    "channel": b.channel or "default",
                    "launch_args": ["--disable-blink-features=AutomationControlled"] if (b.browser_mode or b.provider) == "playwright_stealth" else [],
                    "proxy": mask_proxy_url(b.proxy.get("server") if isinstance(b.proxy, dict) else str(b.proxy or "")) if b.proxy else "none",
                    "user_agent": "default",
                    "batch_size": spec.site.batch_size,
                    "nav_timeout_s": b.nav_timeout_s,
                    "response_timeout_s": spec.site.response_timeout_s,
                    "idle_window_s": spec.site.idle_window_s,
                }
        except Exception as exc:
            carrier_configs["error"] = f"failed to inspect carrier configs: {exc}"

    # Proxies
    proxies = {
        "HTTP_PROXY": mask_proxy_url(os.environ.get("HTTP_PROXY")),
        "HTTPS_PROXY": mask_proxy_url(os.environ.get("HTTPS_PROXY")),
        "NO_PROXY": os.environ.get("NO_PROXY", ""),
        "ALL_PROXY": mask_proxy_url(os.environ.get("ALL_PROXY")),
    }

    try:
        current_user = getpass.getuser()
    except Exception:
        current_user = "unknown"

    return {
        "python": {
            "version": sys.version.replace("\n", " "),
            "executable": sys.executable,
            "platform": platform.platform(),
            "architecture": platform.machine(),
        },
        "system": {
            "user": current_user,
            "cwd": os.getcwd(),
            "session_name": os.environ.get("SESSIONNAME", "<none>"),
            "user_domain": os.environ.get("USERDOMAIN", "<none>"),
            "stdout_isatty": getattr(sys.stdout, "isatty", lambda: False)(),
        },
        "packages": {
            "playwright": get_package_version("playwright"),
            "patchright": get_package_version("patchright"),
            "playwright_stealth": get_package_version("playwright_stealth") or get_package_version("playwright-stealth"),
            "scrapling": get_package_version("scrapling"),
        },
        "browser_binaries": {
            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "<unset>"),
            "chromium_executable_path": chromium_path,
            "chromium_executable_exists": chromium_exists,
        },
        "event_loop": {
            "policy": loop_policy_name,
            "loop_class": loop_class,
            "status": loop_status,
            "is_proactor_on_windows": is_proactor if is_windows else "n/a (non-windows)",
        },
        "storage": {
            "logs_dir": str(logs_dir),
            "logs_free_space_gb": logs_free_gb,
            "logs_dir_writable": logs_writable,
            "logs_dir_status": logs_writable_msg,
            "temp_dir": temp_dir,
            "temp_dir_writable": temp_writable,
            "temp_dir_status": temp_writable_msg,
        },
        "proxies": proxies,
        "carriers": carrier_configs,
    }


def format_environment_report_text(rep: dict[str, Any]) -> str:
    """Format report dictionary into a clean, human-readable multi-line log block."""
    lines = [
        "--- ENVIRONMENT REPORT ---",
        f"Python: {rep['python']['version']} | Executable: {rep['python']['executable']}",
        f"Platform: {rep['python']['platform']} ({rep['python']['architecture']})",
        f"User: {rep['system']['user']} | Domain: {rep['system']['user_domain']} | Session: {rep['system']['session_name']} | CWD: {rep['system']['cwd']} | isatty={rep['system']['stdout_isatty']}",
        f"Packages: playwright={rep['packages']['playwright']}, patchright={rep['packages']['patchright']}, playwright_stealth={rep['packages']['playwright_stealth']}, scrapling={rep['packages']['scrapling']}",
        f"Browsers: PLAYWRIGHT_BROWSERS_PATH={rep['browser_binaries']['PLAYWRIGHT_BROWSERS_PATH']} | Chromium: {rep['browser_binaries']['chromium_executable_path']} (exists={rep['browser_binaries']['chromium_executable_exists']})",
        f"Asyncio Loop: {rep['event_loop']['status']}",
        f"Storage: logs_dir='{rep['storage']['logs_dir']}' (free={rep['storage']['logs_free_space_gb']} GB, {rep['storage']['logs_dir_status']}) | temp_dir='{rep['storage']['temp_dir']}' ({rep['storage']['temp_dir_status']})",
        f"Proxies: HTTP_PROXY='{rep['proxies']['HTTP_PROXY']}', HTTPS_PROXY='{rep['proxies']['HTTPS_PROXY']}', NO_PROXY='{rep['proxies']['NO_PROXY']}'",
    ]
    if rep.get("carriers"):
        lines.append("Carriers Effective Config:")
        for k, v in rep["carriers"].items():
            if isinstance(v, dict):
                lines.append(
                    f"  - {k}: headless={v.get('headless')} (config={v.get('configured_headless')}), mode={v.get('browser_mode')}, channel={v.get('channel')}, batch={v.get('batch_size')}, nav_timeout={v.get('nav_timeout_s')}s, resp_timeout={v.get('response_timeout_s')}s, proxy={v.get('proxy')}"
                )
            else:
                lines.append(f"  - {k}: {v}")
    lines.append("--- END ENVIRONMENT REPORT ---")
    return "\n".join(lines)


def log_environment_report(settings: Any = None, resolver: Any = None, log_obj: logging.Logger = logger) -> dict[str, Any]:
    """Generate and log environment report once per run."""
    report = get_environment_report(settings, resolver)
    formatted = format_environment_report_text(report)
    log_obj.info("environment report\n%s", formatted, extra={"fields": {"env_report": report}})
    return report


# -----------------------------------------------------------------------------
# Task 2: Step-by-Step Lifecycle Tracing
# -----------------------------------------------------------------------------

CANONICAL_STEPS: dict[int, str] = {
    1: "start playwright driver",
    2: "launch browser",
    3: "create context",
    4: "apply stealth / patches",
    5: "new page",
    6: "attach listeners",
    7: "navigate to tracking URL",
    8: "wait for input/submit controls",
    9: "enter PROs",
    10: "click submit",
    11: "wait for API response",
    12: "parse response JSON per PRO",
    13: "close page/context/browser",
}

_NAME_TO_STEP: dict[str, int] = {v.lower(): k for k, v in CANONICAL_STEPS.items()}


class traced_step:
    """Async and sync context manager for tracing lifecycle steps with exact timing and error logging."""

    def __init__(
        self,
        step: int | str,
        name: str | None = None,
        total: int = 13,
        log_obj: logging.Logger | None = None,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        if isinstance(step, int):
            self.step_num = step
            self.name = name or CANONICAL_STEPS.get(step, f"step_{step}")
        else:
            clean = str(step).strip()
            self.name = name or clean
            self.step_num = _NAME_TO_STEP.get(clean.lower(), 0)
        self.total = total
        self.logger = log_obj or logger
        self.extra = dict(extra or {})
        self.extra.update(kwargs)
        self.ok_extra: dict[str, Any] = {}
        self.t0: float = 0.0

    def set_info(self, **kwargs: Any) -> None:
        """Add info to be logged on OK."""
        self.ok_extra.update(kwargs)

    def _format_extra(self, d: dict[str, Any]) -> str:
        if not d:
            return ""
        items = []
        for k, v in d.items():
            if isinstance(v, (list, tuple)):
                v_str = ",".join(str(x) for x in v)
                if len(v_str) > 60:
                    v_str = f"[{len(v)} items]"
            else:
                v_str = str(v)
            items.append(f"{k}={v_str}")
        return " | " + " ".join(items)

    def _enter(self) -> traced_step:
        self.t0 = time.monotonic()
        detail = self._format_extra(self.extra)
        self.logger.info("[STEP %d/%d] %s START%s", self.step_num, self.total, self.name, detail)
        return self

    def _exit(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        elapsed_ms = (time.monotonic() - self.t0) * 1000
        if exc_type is None:
            combined = dict(self.extra)
            combined.update(self.ok_extra)
            detail = self._format_extra(combined)
            self.logger.info(
                "[STEP %d/%d] %s OK elapsed_ms=%.1f%s",
                self.step_num, self.total, self.name, elapsed_ms, detail,
            )
            return False

        error_type = exc_type.__name__
        error_msg = str(exc_val)[:500]
        setattr(exc_val, "failed_step", self.name)
        setattr(exc_val, "failed_step_num", self.step_num)
        setattr(exc_val, "elapsed_ms", elapsed_ms)
        self.logger.error(
            "[STEP %d/%d] %s FAILED elapsed_ms=%.1f error_type=%s error_message=%s",
            self.step_num, self.total, self.name, elapsed_ms, error_type, error_msg,
            exc_info=(exc_type, exc_val, exc_tb),
        )
        return False

    def __enter__(self) -> traced_step:
        return self._enter()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return self._exit(exc_type, exc_val, exc_tb)

    async def __aenter__(self) -> traced_step:
        return self._enter()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return self._exit(exc_type, exc_val, exc_tb)


# -----------------------------------------------------------------------------
# Task 4: Failure Artifacts & Block Page Detection
# -----------------------------------------------------------------------------

async def save_failure_artifacts(
    page: Any,
    context: Any,
    run_id: str,
    carrier_code: str,
    step_name: str,
    diagnostics_dir: str | Path = "logs/diagnostics",
) -> dict[str, str]:
    """Capture screenshot, full HTML, metadata, and optional trace zip on step failure."""
    saved: dict[str, str] = {}
    if not page:
        return saved

    try:
        clean_run = re.sub(r"[^\w.-]+", "_", run_id or "norun")
        clean_carrier = re.sub(r"[^\w.-]+", "_", carrier_code or "carrier")
        clean_step = re.sub(r"[^\w.-]+", "_", step_name or "step")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        out_dir = Path(diagnostics_dir) / clean_run / f"{clean_carrier}_{clean_step}_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1. Screenshot (.png)
        try:
            screenshot_path = out_dir / "screenshot.png"
            await page.screenshot(path=str(screenshot_path), full_page=True, timeout=5000)
            saved["screenshot"] = str(screenshot_path)
        except Exception as exc:
            saved["screenshot"] = f"failed: {exc}"

        # 2. Page HTML (.html)
        try:
            html_path = out_dir / "page.html"
            content = await page.content()
            html_path.write_text(content, encoding="utf-8")
            saved["html"] = str(html_path)
        except Exception as exc:
            saved["html"] = f"failed: {exc}"

        # 3. URL and Title (.txt)
        try:
            info_path = out_dir / "info.txt"
            cur_url = page.url
            title = await page.title()
            info_text = f"timestamp: {datetime.now(timezone.utc).isoformat()}\ncarrier: {carrier_code}\nstep: {step_name}\nurl: {cur_url}\ntitle: {title}\n"
            info_path.write_text(info_text, encoding="utf-8")
            saved["info"] = str(info_path)
        except Exception as exc:
            saved["info"] = f"failed: {exc}"

        # 4. Optional Playwright trace zip if SCRAPER_TRACE=1
        if os.environ.get("SCRAPER_TRACE", "0").lower() in ("1", "true", "yes") and context:
            try:
                trace_path = out_dir / "trace.zip"
                await context.tracing.stop(path=str(trace_path))
                saved["trace"] = str(trace_path)
            except Exception as exc:
                saved["trace"] = f"failed: {exc}"

        logger.info(
            "failure artifacts saved | carrier=%s step=%s artifacts=%s",
            carrier_code,
            step_name,
            saved,
        )
    except Exception as exc:
        logger.warning("failed to save failure artifacts: %s", exc)

    return saved


def check_block_page(
    page_text: str | None,
    url: str | None,
    status: int | None,
    title: str | None,
) -> str | None:
    """Check if the response or page text matches anti-bot / block page patterns."""
    if status in (403, 429) or (status and status >= 500):
        return f"HTTP {status}"

    combined = f"{title or ''} {url or ''} {page_text or ''}".lower()
    for marker in BLOCK_PAGE_MARKERS:
        if marker in combined:
            return f"marker: {marker}"
    return None


# -----------------------------------------------------------------------------
# Task 3: Exception Hooks & Stderr Capture
# -----------------------------------------------------------------------------

def install_exception_hooks(log_obj: logging.Logger = logger) -> None:
    """Install sys.excepthook, threading.excepthook, and asyncio exception handler."""

    def _sys_hook(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        log_obj.critical(
            "Uncaught sys exception: %s: %s",
            exc_type.__name__,
            exc_value,
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    sys.excepthook = _sys_hook

    def _thread_hook(args):
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        log_obj.critical(
            "Uncaught thread exception in thread '%s': %s: %s",
            args.thread.name if args.thread else "unknown",
            args.exc_type.__name__,
            args.exc_value,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook


def setup_asyncio_exception_handler(loop: asyncio.AbstractEventLoop, log_obj: logging.Logger = logger) -> None:
    """Attach unhandled exception handler to an active asyncio event loop."""
    def _async_handler(lp, context):
        msg = context.get("message", "Unhandled asyncio exception")
        exc = context.get("exception")
        log_obj.error("Asyncio unhandled exception: %s (exc=%s)", msg, exc, exc_info=exc)

    loop.set_exception_handler(_async_handler)


class StderrTee(io.TextIOBase):
    """Tee stderr stream to both original stderr and python logger."""

    def __init__(self, original_stderr, log_func):
        self.original_stderr = original_stderr
        self.log_func = log_func
        self._buffer = ""

    def write(self, s):
        try:
            self.original_stderr.write(s)
            self.original_stderr.flush()
        except Exception:
            pass

        self._buffer += s
        if "\n" in self._buffer:
            lines = self._buffer.split("\n")
            for line in lines[:-1]:
                line_str = line.strip()
                if line_str:
                    self.log_func(line_str)
            self._buffer = lines[-1]
        return len(s)

    def flush(self):
        try:
            self.original_stderr.flush()
        except Exception:
            pass
        if self._buffer.strip():
            self.log_func(self._buffer.strip())
            self._buffer = ""
