"""Structured logging for the web tier.

The webapp/ package previously had zero ``import logging`` — production
failures vanished into uvicorn's access log or silent ``except Exception``
blocks. This module gives every webapp module one consistent logger:

- JSON lines on stdout (Railway parses these natively; locally use --text)
- a request-id middleware so any error can be correlated to its request

Usage: ``from .log import get_logger; log = get_logger(__name__)``
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            doc["exc"] = self.formatException(record.exc_info)[-2000:]
        for key in ("request_id", "path", "method", "status", "duration_ms"):
            if hasattr(record, key):
                doc[key] = getattr(record, key)
        return json.dumps(doc, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """Idempotent config: JSON handler on the 'webapp' namespace AND the
    'src' namespace (scheduler/refresh pipelines) so their failures — e.g. a
    silently-dead scheduler thread — are visible in container logs."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    for ns in ("webapp", "src"):
        lg = logging.getLogger(ns)
        lg.setLevel(level)
        lg.addHandler(handler)
        lg.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    if not name.startswith("webapp"):
        name = f"webapp.{name}"
    return logging.getLogger(name)


async def request_logging_middleware(request, call_next):
    """Attach X-Request-ID + timing to every response; log non-2xx."""
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    import time
    t0 = time.perf_counter()
    response = await call_next(request)
    dur_ms = round((time.perf_counter() - t0) * 1000, 1)
    response.headers["X-Request-ID"] = rid
    if response.status_code >= 400:
        get_logger("http").warning(
            "%s %s -> %s", request.method, request.url.path, response.status_code,
            extra={"request_id": rid, "path": request.url.path,
                   "method": request.method, "status": response.status_code,
                   "duration_ms": dur_ms})
    return response
