"""Bootstrap fail-fast semantics: the 23-Aug Railway outage class.

bootstrap used to exit 0 even when every boot-critical R2 fetch failed, so
uvicorn started into an empty database and /api/health crash-looped the
container with no visible cause. Now: misconfigured readonly deploys FAIL
the start command where the reason is visible."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def boot(tmp_path, monkeypatch):
    import deploy.bootstrap as bs

    monkeypatch.setattr(bs, "DATA", tmp_path)          # isolated data dir
    monkeypatch.setattr(bs, "load_env", lambda: None)  # ignore deploy/.env
    monkeypatch.delenv("MF_READONLY_DB", raising=False)
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
              "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    return bs


def test_dev_mode_skips(boot, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    assert boot.main() == 0


def test_readonly_without_r2_fails_loudly(boot, monkeypatch, capfd):
    monkeypatch.setenv("MF_READONLY_DB", "1")
    assert boot.main() == 1
    out = capfd.readouterr().out
    assert "R2 variables" in out and "MISSING" in out


def test_readonly_with_r2_but_failed_fetch_fails(boot, monkeypatch, capfd):
    """The exact Railway-outage shape: creds present but fetch fails -> exit 1."""
    bs = boot
    monkeypatch.setenv("MF_READONLY_DB", "1")
    for i, k in enumerate(("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                           "R2_SECRET_ACCESS_KEY", "R2_BUCKET")):
        monkeypatch.setenv(k, f"bogus{i}")
    monkeypatch.setattr(bs, "BOOT_CRITICAL", ["webapp.db"])
    monkeypatch.setattr(bs, "download_to", lambda key, dest: None)  # all fail
    # ensure_prefix must not explode either
    monkeypatch.setattr(bs, "ensure_prefix", lambda prefix: 0)
    # download_to is referenced inside main via direct name; patch module attr
    assert bs.main() == 1
    out = capfd.readouterr().out
    assert "BOOTSTRAP FAILED" in out and "webapp.db" in out


def test_readonly_with_successful_fetch_passes(boot, monkeypatch):
    bs = boot
    monkeypatch.setenv("MF_READONLY_DB", "1")
    for i, k in enumerate(("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                           "R2_SECRET_ACCESS_KEY", "R2_BUCKET")):
        monkeypatch.setenv(k, f"ok{i}")

    def fake_download(key, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return True

    monkeypatch.setattr(bs, "download_to", fake_download)
    monkeypatch.setattr(bs, "ensure_prefix", lambda prefix: 0)
    assert bs.main() == 0
