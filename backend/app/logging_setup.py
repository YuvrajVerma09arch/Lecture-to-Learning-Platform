"""Structured logging.

Rules this module enforces:
  * Secrets never reach a log record.
  * Transcript text, note text, and student questions never reach a log
    record. We log lengths, counts, IDs, and durations instead.
  * Every record carries a correlation id so a turn can be reconstructed.

The redaction filter is a backstop, not a substitute for care at the call
site: it catches accidental interpolation of a key into a message.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

_SENSITIVE_KEYS = re.compile(
    r"(api[_-]?key|api[_-]?secret|authorization|token|password|secret|credential)",
    re.IGNORECASE,
)
# Anything that looks like an inline key assignment, e.g. "key=abc123".
_INLINE_SECRET = re.compile(
    r"((?:api[_-]?key|api[_-]?secret|token|password|secret)\s*[=:]\s*)\S+",
    re.IGNORECASE,
)


class RedactingFilter(logging.Filter):
    """Makes log records safe to emit.

    Three layers:
      1. Structured ``extra`` fields whose names look sensitive are dropped.
      2. Free-text messages are scanned for inline key=value pairs and the
         value is masked.
      3. ``extra`` keys that collide with a built-in ``LogRecord``
         attribute are renamed. Logging raises ``KeyError`` on a
         collision, so a stray ``extra={"filename": ...}`` would otherwise
         turn a log call into a crash inside a request handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Drop sensitive ``extra`` fields entirely rather than masking them,
        # so their length does not leak either.
        for key in list(record.__dict__.keys()):
            if key.startswith("_") or key in _STANDARD_ATTRS:
                continue
            if _SENSITIVE_KEYS.search(key):
                record.__dict__[key] = "***redacted***"

        if isinstance(record.msg, str):
            record.msg = _INLINE_SECRET.sub(r"\1***redacted***", record.msg)
        return True


_ORIGINAL_MAKE_RECORD = logging.Logger.makeRecord


def _safe_make_record(
    self, name, level, fn, lno, msg, args, exc_info, func=None, extra=None, sinfo=None
):
    """``Logger.makeRecord`` that tolerates colliding ``extra`` keys.

    The standard implementation raises ``KeyError: "Attempt to overwrite
    'filename' in LogRecord"`` when an ``extra`` key collides with a
    built-in attribute. In a request handler that turns a log line into a
    500, so the colliding key is moved aside under an ``x_`` prefix and the
    data is preserved rather than dropped.
    """
    if extra:
        safe = {}
        for key, value in extra.items():
            if key in _STANDARD_ATTRS and not key.startswith("_"):
                key = f"x_{key}"
            safe[key] = value
        extra = safe
    return _ORIGINAL_MAKE_RECORD(
        self, name, level, fn, lno, msg, args, exc_info, func=func, extra=extra, sinfo=sinfo
    )


def _install_safe_make_record() -> None:
    """Install the safe ``makeRecord`` once, at import time."""
    if logging.Logger.makeRecord is not _safe_make_record:
        logging.Logger.makeRecord = _safe_make_record


_install_safe_make_record()


_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Emits one JSON object per line, with a stable field set."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _STANDARD_ATTRS:
                continue
            if key in ("exc_info", "exc_text", "stack_info"):
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # These are chatty and can contain payload fragments.
    for noisy in ("httpx", "httpcore", "aiohttp.access", "websockets.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
