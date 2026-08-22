"""Refresh-pipeline telemetry: append-only JSONL event log.

Every data-refresh entry point (NAV daily, AMFI tier-1 fetch, bond catalog,
stock chain) records started/success/error events here so the superadmin
screen can show what ran, when, how long it took, and why something failed.

Store: data/logs/refresh_log.jsonl  (gitignored; per-instance).
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_PATH = BASE_DIR / "data" / "logs" / "refresh_log.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")

_lock = threading.Lock()


def _write(entry: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _lock:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass  # telemetry must never break a pipeline


def record(pipeline: str, status: str, **fields) -> None:
    entry = {"ts": _now_ist(),
             "pipeline": pipeline, "status": status}
    entry.update({k: v for k, v in fields.items() if v is not None})
    _write(entry)


@contextmanager
def track(pipeline: str, **meta):
    """Context manager: records started/success/error around a pipeline run.

    Yields a mutable dict â€” callers may update it with result counters which
    are merged into the final success event."""
    t0 = time.time()
    state = dict(meta)
    record(pipeline, "started", **state)
    try:
        yield state
    except Exception as e:  # noqa: BLE001
        record(pipeline, "error", duration_s=round(time.time() - t0, 1),
               error=str(e)[:400], trace=traceback.format_exc()[-1800:], **state)
        raise
    else:
        record(pipeline, "success", duration_s=round(time.time() - t0, 1), **state)


def read(limit: int = 300) -> list[dict]:
    """Newest-first event list."""
    if not LOG_PATH.exists():
        return []
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for ln in lines[-max(1, limit):]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out[::-1]


def summary() -> dict:
    """Per-pipeline rollup for the admin screen (timestamps IST)."""
    events = read(1000)
    pipes: dict[str, dict] = {}
    cutoff = datetime.now().timestamp() - 24 * 3600
    for e in events:
        name = e.get("pipeline") or "?"
        p = pipes.setdefault(name, {
            "last_status": None, "last_ts": None, "last_duration_s": None,
            "last_error": None, "last_detail": {}, "ok_24h": 0, "err_24h": 0,
        })
        try:
            recent = datetime.fromisoformat(e["ts"]).timestamp() >= cutoff
        except Exception:
            recent = False
        # entries may carry a +05:30 offset (IST) or be naive legacy UTC
        if recent and "+" not in e["ts"]:
            try:
                recent = datetime.fromisoformat(e["ts"]).replace(
                    tzinfo=timezone.utc).timestamp() >= cutoff
            except Exception:
                pass
        if e["status"] == "success":
            p["last_status"], p["last_ts"] = "success", e["ts"]
            p["last_duration_s"] = e.get("duration_s")
            p["last_detail"] = {k: v for k, v in e.items()
                                if k not in ("ts", "pipeline", "status", "duration_s")}
            if recent:
                p["ok_24h"] += 1
        elif e["status"] == "error":
            if p["last_status"] != "success":  # newest failure wins unless newer success
                p["last_status"], p["last_ts"] = "error", e["ts"]
                p["last_duration_s"] = e.get("duration_s")
                p["last_error"] = e.get("error")
            if recent:
                p["err_24h"] += 1
    return {"pipelines": pipes, "generated_at": _now_ist()}

