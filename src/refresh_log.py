"""Refresh-pipeline telemetry: append-only JSONL event log + durable state.

Every data-refresh entry point (NAV daily, AMFI tier-1 fetch, bond catalog,
stock chain) records started/success/error events here so the superadmin
screen can show what ran, when, how long it took, and why something failed.

Stores:
- data/logs/refresh_log.jsonl   append-only raw events (gitignored; per-instance)
- data/logs/refresh_state.json  compact per-pipeline rollup ("last fetched"),
  atomically rewritten on every event and best-effort pushed to R2 so the
  last-fetched record survives redeploys (restored on first read).

Telemetry must never break a pipeline: every failure path is swallowed.
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
STATE_PATH = BASE_DIR / "data" / "logs" / "refresh_state.json"

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")

_lock = threading.Lock()
_state_cache: dict | None = None


def _write(entry: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _lock:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass  # telemetry must never break a pipeline


# ---- durable per-pipeline state -------------------------------------------

_STATE_FIELDS = ("last_status", "last_ts", "last_started", "last_duration_s",
                 "last_error", "last_detail")


def _restore_state() -> None:
    """Fresh container: pull the rollup back from R2 (best-effort, no-op local)."""
    try:
        if STATE_PATH.exists():
            return
        from webapp import remote_store
        remote_store.ensure("logs/" + STATE_PATH.name)
    except Exception:
        pass


def read_state() -> dict:
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    _restore_state()
    state: dict = {}
    if STATE_PATH.exists():
        try:
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except Exception:
            state = {}
    _state_cache = state
    return _state_cache


def _push_state() -> None:
    try:
        from webapp import remote_store
        if remote_store.is_configured():
            remote_store.upload_object("logs/" + STATE_PATH.name, STATE_PATH)
    except Exception:
        pass


def _apply_event(pipes: dict, e: dict) -> None:
    name = e.get("pipeline") or "?"
    p = pipes.setdefault(name, {"last_status": None, "last_ts": None,
                                "last_started": None, "last_duration_s": None,
                                "last_error": None, "last_detail": {}})
    status = e.get("status")
    detail = {k: v for k, v in e.items()
              if k not in ("ts", "pipeline", "status", "duration_s")}
    if status == "started":
        p["last_started"] = e.get("ts")
        return
    if status == "success" or status == "alive":
        # 'alive' = scheduler heartbeat [S2f]; treated as success so liveness
        # flows into the standard last-fetched rollup + 24h counters.
        p["last_status"], p["last_ts"] = status, e.get("ts")
    elif status == "error":
        # newest failure wins unless a newer success was already recorded
        if p.get("last_status") != "success":
            p["last_status"], p["last_ts"] = "error", e.get("ts")
        else:
            return
    else:
        return
    p["last_duration_s"] = e.get("duration_s")
    p["last_error"] = e.get("error")
    p["last_detail"] = detail


def _bump_state(entry: dict) -> None:
    pipes = read_state().setdefault("pipelines", {})
    with _lock:
        _apply_event(pipes, entry)
        state = {"pipelines": pipes, "updated_at": _now_ist()}
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(STATE_PATH)
        except Exception:
            return  # telemetry must never break a pipeline
    _push_state()


def record(pipeline: str, status: str, **fields) -> None:
    entry = {"ts": _now_ist(),
             "pipeline": pipeline, "status": status}
    entry.update({k: v for k, v in fields.items() if v is not None})
    _write(entry)
    _bump_state(entry)


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


def parse_ts(ts) -> datetime | None:
    """ISO timestamp -> aware datetime.

    Naive timestamps are legacy entries written before the IST conversion
    (commit 5ff9a65), so they carry IST, not UTC."""
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def _slot(e: dict) -> dict:
    return {"status": e.get("status"), "ts": e.get("ts"),
            "duration_s": e.get("duration_s"), "error": e.get("error"),
            "detail": {k: v for k, v in e.items()
                       if k not in ("ts", "pipeline", "status", "duration_s")}}


def summary() -> dict:
    """Per-pipeline rollup for the admin screen (timestamps IST).

    Built from recent JSONL events (true newest success/error wins, mixing
    naive-UTC legacy and IST entries safely), then merged with the durable
    state file so pipelines whose history predates this instance/redeploy
    still report their real last-fetched values."""
    events = read(1000)
    pipes: dict[str, dict] = {}
    cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600

    def newer(ts, than) -> bool:
        dt, cur = parse_ts(ts), parse_ts(than)
        return dt is not None and (cur is None or dt > cur)

    for e in events:
        name = e.get("pipeline") or "?"
        p = pipes.setdefault(name, {
            "last_status": None, "last_ts": None, "last_duration_s": None,
            "last_error": None, "last_detail": {}, "ok_24h": 0, "err_24h": 0,
            "_success": None, "_error": None,
        })
        try:
            recent = parse_ts(e["ts"]).timestamp() >= cutoff
        except Exception:
            recent = False
        slot = _slot(e)
        if e["status"] == "success":
            if recent:
                p["ok_24h"] += 1
            if p["_success"] is None or newer(slot["ts"], p["_success"]["ts"]):
                p["_success"] = slot
        elif e["status"] == "error":
            if recent:
                p["err_24h"] += 1
            if p["_error"] is None or newer(slot["ts"], p["_error"]["ts"]):
                p["_error"] = slot

    for p in pipes.values():
        s, err = p.pop("_success", None), p.pop("_error", None)
        win = s if (s and (not err
                           or parse_ts(s["ts"]) >= parse_ts(err["ts"]))) else (err or s)
        if win:
            p.update(last_status=win["status"], last_ts=win["ts"],
                     last_duration_s=win["duration_s"],
                     last_error=win["error"], last_detail=win["detail"])

    for name, sp in (read_state().get("pipelines") or {}).items():
        p = pipes.setdefault(name, {
            "last_status": None, "last_ts": None, "last_duration_s": None,
            "last_error": None, "last_detail": {}, "ok_24h": 0, "err_24h": 0,
        })
        s_dt, j_dt = parse_ts(sp.get("last_ts")), parse_ts(p.get("last_ts"))
        if s_dt and (j_dt is None or s_dt > j_dt):
            for k in _STATE_FIELDS:
                if sp.get(k) is not None:
                    p[k] = sp[k]
        elif p.get("last_started") is None and sp.get("last_started"):
            p["last_started"] = sp["last_started"]

    return {"pipelines": pipes, "generated_at": _now_ist(),
            "log_file": str(LOG_PATH), "state_file": str(STATE_PATH)}
