"""
Global settings — things that apply across ALL carriers, as opposed to
per-carrier specifics (which live in app/config/carriers/*.yaml).

Reads from environment variables (via .env locally, real env vars on a
server) with sensible defaults, so behavior can be tuned per-deployment
without touching code.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes")


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val is not None else default


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val is not None else default


# app/logs -- resolved relative to this file's location (app/config/settings.py),
# not the process's working directory, so it's correct no matter where a
# script is launched from.
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_LOG_DIR = os.path.join(_APP_DIR, "logs")


@dataclass(frozen=True)
class Settings:
    headless: bool = _env_bool("SCRAPER_HEADLESS", False)
    max_retries: int = _env_int("SCRAPER_MAX_RETRIES", 3)
    min_delay_seconds: float = _env_float("SCRAPER_MIN_DELAY", 2.0)
    max_delay_seconds: float = _env_float("SCRAPER_MAX_DELAY", 5.0)
    response_timeout_ms: int = _env_int("SCRAPER_RESPONSE_TIMEOUT_MS", 20000)
    log_dir: str = os.getenv("LOG_DIR", _DEFAULT_LOG_DIR)
    log_level: str = os.getenv("LOG_LEVEL", "DEBUG")

    # recurring-job scheduling
    schedule_interval_hours: float = _env_float("SCHEDULE_INTERVAL_HOURS", 6.0)
    schedule_jitter_minutes: float = _env_float("SCHEDULE_JITTER_MINUTES", 25.0)


settings = Settings()
