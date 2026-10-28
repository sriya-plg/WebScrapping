"""Structured JSON logs with secret redaction (registered secrets + key=value patterns)."""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import json
import logging
from pathlib import Path
import re
import sys
import uuid
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


class FlushFileHandler(logging.FileHandler):
    """FileHandler that flushes after every record so crashes never lose trailing logs."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


def setup_logging(level: str = "INFO", log_format: str = "console", logs_dir: str | Path = "logs") -> None:
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    h = logging.StreamHandler(sys.stdout)
    fmt = (log_format or "console").lower().strip()
    h.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        from app.core.diagnostics import install_exception_hooks
        install_exception_hooks(root)
    except Exception:
        pass


@contextmanager
def run_file_logger(run_id: str | None = None, logs_dir: str | Path = "logs", level: str | None = None):
    """Context manager attaching a per-run timestamped log file in logs_dir to root logger."""
    p = Path(logs_dir)
    p.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    rid = run_id or run_id_var.get()
    if not rid or rid == "-":
        rid = uuid.uuid4().hex[:12]
        run_id_var.set(rid)

    filename = f"run_{ts}_{rid}.log"
    log_file = p / filename

    handler = FlushFileHandler(str(log_file), encoding="utf-8")
    handler.setFormatter(ConsoleFormatter())
    if level:
        handler.setLevel(level)

    root = logging.getLogger()
    orig_root_level = root.level
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    root.addHandler(handler)

    try:
        from app.core.diagnostics import install_exception_hooks, StderrTee
        install_exception_hooks(root)
        orig_stderr = sys.stderr
        sys.stderr = StderrTee(orig_stderr, lambda msg: root.error("stderr | %s", msg))
        stderr_redirected = True
    except Exception:
        orig_stderr = sys.stderr
        stderr_redirected = False

    try:
        yield log_file
    finally:
        if stderr_redirected:
            try:
                sys.stderr.flush()
                sys.stderr = orig_stderr
            except Exception:
                pass
        try:
            handler.flush()
            root.removeHandler(handler)
            handler.close()
            root.setLevel(orig_root_level)
        except Exception:
            pass


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})
