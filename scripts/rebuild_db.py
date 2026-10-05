"""Rebuild the frontend SQLite DB from the parsed corpus.

``build_db`` compares a fingerprint of the data/ sources against the one stored
in the ``meta`` table and only rebuilds when they differ, so calling it after a
parse run is the normal way to publish new data to the app.
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from webapp.db import build_db  # noqa: E402


def main() -> int:
    force = "--force" in sys.argv
    path = build_db(force=force)
    print(f"webapp.db: {path}")
    print(f"exists={path.exists()} size={path.stat().st_size:,}b force={force}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())