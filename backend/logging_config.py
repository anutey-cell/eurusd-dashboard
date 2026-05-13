"""
Structured JSON logging for all backend modules.

Call setup_logging() once from main.py lifespan before serving requests.
Every log line is a single-line JSON object, machine-parseable by Datadog,
Loki, CloudWatch, etc., while still being human-readable with `jq`.

Output shape:
  {"time": "2026-05-13T18:00:00Z", "level": "INFO", "name": "routers.signal",
   "message": "Signal analyzed: BUY quality=85", "request_id": "abc123"}
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "time":    datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "name":    record.name,
            "message": record.getMessage(),
        }
        # Thread-local request ID injected by middleware (optional)
        rid = getattr(record, "request_id", None)
        if rid:
            obj["request_id"] = rid

        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)

        return json.dumps(obj, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    """
    Configure root logger with JSON output.
    Quiets noisy third-party loggers that would flood the stream.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Third-party noise reduction
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
