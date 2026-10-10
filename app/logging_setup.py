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


def _record_fields(r: logging.LogRecord) -> dict:
    fields = dict(getattr(r, "fields", {}) or {})
    for key, value in r.__dict__.items():
        if key in ("name", "msg", "args", "created", "filename", "funcName", "levelname", "levelno",
                   "lineno", "module", "msecs", "message", "pathname", "process", "processName",
                   "relativeCreated", "thread", "threadName", "exc_info", "exc_text", "stack_info",
                   "taskName", "fields"):
            continue
        if key.startswith("_"):
            continue
        fields.setdefault(key, value)
    return fields


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": datetime.now(timezone.utc).isoformat(), "level": r.levelname, "logger": r.name,
             "run_id": run_id_var.get(), "client": client_var.get(), "carrier": carrier_var.get(), "msg": r.getMessage()}
        d.update(_record_fields(r))
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        d = {k: redact(v) if isinstance(v, str) else v for k, v in d.items()}
        return json.dumps(d, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable single-line logs for local runs."""

    def format(self, r: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(r.created, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        ctx_parts = []
        run_id = run_id_var.get()
        client = client_var.get()
        carrier = carrier_var.get()
        if run_id and run_id != "-":
            ctx_parts.append(f"run={run_id}")
        if client and client != "-":
            ctx_parts.append(f"client={client}")
        if carrier and carrier != "-":
            ctx_parts.append(f"carrier={carrier}")
        ctx = f" [{' '.join(ctx_parts)}]" if ctx_parts else ""
        msg = redact(r.getMessage())
        fields = _record_fields(r)
        if fields:
            detail = " ".join(f"{k}={redact(str(v))}" for k, v in fields.items())
            msg = f"{msg} | {detail}"
        line = f"{ts} {r.levelname:5} {r.name}{ctx} | {msg}"
        if r.exc_info:
            line = f"{line}\n{self.formatException(r.exc_info)}"
        return line


def setup_logging(level: str = "INFO", log_format: str = "console") -> None:
    h = logging.StreamHandler(sys.stdout)
    fmt = (log_format or "console").lower().strip()
    h.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})
