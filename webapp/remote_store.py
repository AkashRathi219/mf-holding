"""Best-effort R2-backed storage for deployment.

The webapp's runtime JSON (``data/nav_history``, ``data/stock_history``,
``data/stock_actions``, ``data/stock_reports``, ``data/reference``) cannot ship
in the Git repo or on Railway's 1 GB ephemeral disk, so those live in a
Cloudflare R2 bucket (S3 API compatible, keyed ``db/<data-relative path>``).

Every read in this fallback is **optional**: when the R2 env vars are absent
or a fetch fails, the app degrades to whatever exists locally (its local-dev
behaviour). Nothing in this module ever raises out of an ``ensure()`` call.

Env (set on Railway; optional locally):
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

_lock = threading.Lock()
_client = None
_configured = None

ENV_KEYS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


def is_configured() -> bool:
    global _configured
    if _configured is None:
        _configured = all(os.environ.get(k) for k in ENV_KEYS)
    return _configured


def _get_client():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client(
            "s3",
            endpoint_url=(
                "https://"
                + os.environ["R2_ACCOUNT_ID"]
                + ".r2.cloudflarestorage.com"
            ),
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        )
    return _client


def _bucket() -> str:
    return os.environ["R2_BUCKET"]


def _key(relpath: str) -> str:
    prefix = os.environ.get("R2_PREFIX", "db").strip("/")
    return (prefix + "/" + relpath.strip("/")).lstrip("/")


def ensure(relpath: str, dest: Path | None = None) -> Path | None:
    """Guarantee ``data/<relpath>`` exists, fetching from R2 if necessary.

    Returns the local path on success; None when not available (unconfigured
    R2 or download failed). Callers must tolerate None gracefully.
    """
    if not is_configured():
        return None
    target = dest or (DATA_DIR / relpath)
    if target.exists():
        return target
    with _lock:
        if target.exists():
            return target
        try:
            resp = _get_client().get_object(Bucket=_bucket(), Key=_key(relpath))
            body = resp["Body"].read()
        except Exception:
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(target)
        except OSError:
            return None
        return target if target.exists() else None


def download_to(relpath: str, dest: Path) -> Path | None:
    """Fetch an object unconditionally to an explicit destination."""
    if not is_configured():
        return None
    try:
        resp = _get_client().get_object(Bucket=_bucket(), Key=_key(relpath))
        body = resp["Body"].read()
    except Exception:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(body)
    tmp.replace(dest)
    return dest
