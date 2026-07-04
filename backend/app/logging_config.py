"""Central logging setup.

Logger namespace ``healthlog`` with its own stderr handler and
``propagate=False``; modules use ``healthlog.api`` / ``healthlog.ingest`` /
``healthlog.scheduler``. Uniform second-precision format, optional JSON.
Kept dependency-free and English-only (operator-facing).
"""

from __future__ import annotations

import json
import logging
import logging.config

_DATEFMT = "%Y-%m-%d %H:%M:%S"
_TEXTFMT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class _JsonFormatter(logging.Formatter):
    """One JSON object per line. Fields: time/level/logger/message."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, _DATEFMT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXTFMT, datefmt=_DATEFMT))

    root_app = logging.getLogger("healthlog")
    root_app.handlers.clear()
    root_app.addHandler(handler)
    root_app.setLevel(level.upper())
    root_app.propagate = False

    # uvicorn access logs are noise per-request; pin to WARNING. Errors still
    # surface through uvicorn.error.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def safe(value: object, *, max_len: int = 256) -> str:
    """Sanitise an externally-influenced value for plain-text logging.

    Strips CR/LF and other control characters so a crafted error string can't
    forge extra log lines, and bounds the length.
    """
    s = "" if value is None else str(value)
    s = "".join(" " if (c == "\n" or c == "\r" or ord(c) < 32) else c for c in s)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s
