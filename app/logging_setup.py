"""Structured JSON logs with secret redaction (registered secrets + key=value patterns)."""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
carrier_var: contextvars.ContextVar[str] = contextvars.ContextVar("carrier", default="-")
client_var: contextvars.ContextVar[str] = contextvars.ContextVar("client", default="-")

_SECRETS: set[str] = set()
_PATTERN = re.compile(
    r"(?i)\b(password|passwd|token|authorization|cookie|set-cookie|csrf[-_a-z]*|api[_-]?key|cf_clearance)"
    r"(\"?\s*[:=]\s*\"?)([^\s\",;&]+)")


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    return _PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": datetime.now(timezone.utc).isoformat(), "level": r.levelname, "logger": r.name,
             "run_id": run_id_var.get(), "client": client_var.get(), "carrier": carrier_var.get(), "msg": r.getMessage()}
        d.update(getattr(r, "fields", {}) or {})
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        d = {k: redact(v) if isinstance(v, str) else v for k, v in d.items()}
        return json.dumps(d, default=str)


def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})
