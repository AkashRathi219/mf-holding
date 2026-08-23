"""S5: captcha key lives in an env var named by settings.yaml — never inline."""

from __future__ import annotations

from pathlib import Path

import yaml


def _cfg_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


def _captcha_cfg():
    doc = yaml.safe_load(_cfg_path().read_text(encoding="utf-8")) or {}
    return doc.get("captcha") or {}


def test_settings_yaml_has_no_inline_key():
    cfg = _captcha_cfg()
    assert cfg.get("api_key", None) in ("", None), (
        "captcha.api_key must stay empty in settings.yaml [S5]")
    assert cfg.get("api_key_env"), "api_key_env indirection required [S5]"


def test_solver_reads_env(monkeypatch):
    from src.captcha_solver import CaptchaSolver
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    assert CaptchaSolver.from_config({}).is_configured() is False
    monkeypatch.setenv("CAPSOLVER_API_KEY", "CAP-TEST-KEY")
    solver = CaptchaSolver.from_config({})
    assert solver.is_configured() is True and solver.api_key == "CAP-TEST-KEY"


def test_explicit_value_still_wins():
    from src.captcha_solver import CaptchaSolver
    s = CaptchaSolver.from_config({"api_key": "explicit", "api_key_env": "NOPE"})
    assert s.api_key == "explicit"
