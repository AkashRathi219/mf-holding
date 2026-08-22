"""hCaptcha solving via a third-party solving service (CapSolver / 2captcha).

Configuration lives in config/settings.yaml:

    captcha:
      service: capsolver          # "capsolver" or "2captcha"
      api_key: YOUR_KEY_HERE      # leave empty to disable

Getting a key:
  - CapSolver: https://dashboard.capsolver.com -> "API Keys" -> copy the key.
  - 2captcha:  https://2captcha.com -> "API Settings" -> copy the key.

The solver is only used by AMC adapters whose sites are protected by an
interactive captcha (e.g. Kotak's PerfDrive/hCaptcha challenge).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config" / "settings.yaml"

CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"

TWO_CAPTCHA_IN = "https://2captcha.com/in.php"
TWO_CAPTCHA_RES = "https://2captcha.com/res.php"


def load_captcha_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return {}
    return cfg.get("captcha", {}) or {}


class CaptchaSolver:
    """Solve hCaptcha challenges using a configured third-party service."""

    def __init__(self, service: str = "capsolver", api_key: str = "", timeout: int = 180):
        self.service = (service or "capsolver").lower()
        self.api_key = api_key or ""
        self.timeout = timeout

    @classmethod
    def from_config(cls, config: dict | None = None) -> "CaptchaSolver":
        cfg = config if config is not None else load_captcha_config()
        return cls(service=cfg.get("service", "capsolver"),
                   api_key=cfg.get("api_key", ""),
                   timeout=int(cfg.get("timeout", 180)))

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def solve_hcaptcha(self, sitekey: str, page_url: str) -> str | None:
        """Return a solved hCaptcha token, or None if not configured/failed."""
        if not self.is_configured():
            logger.warning(
                "Captcha solver not configured - add `captcha.api_key` to "
                "config/settings.yaml (see src/captcha_solver.py)"
            )
            return None
        if not sitekey or not page_url:
            logger.error("hCaptcha solve requires both sitekey and page_url")
            return None

        if self.service == "capsolver":
            return self._solve_capsolver(sitekey, page_url)
        if self.service == "2captcha":
            return self._solve_2captcha(sitekey, page_url)
        logger.error(f"Unsupported captcha service: {self.service}")
        return None

    # ---- CapSolver ----
    def _solve_capsolver(self, sitekey: str, page_url: str) -> str | None:
        try:
            r = httpx.post(CAPSOLVER_CREATE, json={
                "clientKey": self.api_key,
                "task": {
                    "type": "HCaptchaTaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                },
            }, timeout=30)
            task_id = r.json().get("taskId")
            if not task_id:
                logger.error(f"CapSolver createTask failed: {r.text[:300]}")
                return None
        except Exception as e:
            logger.error(f"CapSolver createTask error: {e}")
            return None

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(5)
            try:
                r = httpx.post(CAPSOLVER_RESULT, json={
                    "clientKey": self.api_key,
                    "taskId": task_id,
                }, timeout=30)
                data = r.json()
            except Exception as e:
                logger.error(f"CapSolver getTaskResult error: {e}")
                return None
            if data.get("status") == "ready":
                token = data.get("solution", {}).get("gRecaptchaResponse")
                if token:
                    return token
            elif data.get("status") == "failed":
                logger.error(f"CapSolver task failed: {data.get('errorDescription')}")
                return None

        logger.error("CapSolver task timed out")
        return None

    # ---- 2captcha ----
    def _solve_2captcha(self, sitekey: str, page_url: str) -> str | None:
        try:
            r = httpx.post(TWO_CAPTCHA_IN, data={
                "key": self.api_key,
                "method": "hcaptcha",
                "sitekey": sitekey,
                "pageurl": page_url,
                "json": 1,
            }, timeout=30)
            data = r.json()
            cap_id = data.get("request")
            if data.get("status") != 1 or not cap_id:
                logger.error(f"2captcha submit failed: {data}")
                return None
        except Exception as e:
            logger.error(f"2captcha submit error: {e}")
            return None

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(5)
            try:
                r = httpx.get(TWO_CAPTCHA_RES, params={
                    "key": self.api_key,
                    "action": "get",
                    "id": cap_id,
                    "json": 1,
                }, timeout=30)
                data = r.json()
            except Exception as e:
                logger.error(f"2captcha get error: {e}")
                return None
            if data.get("status") == 1:
                return data.get("request")
            if data.get("request") == "CAPCHA_NOT_READY":
                continue
            logger.error(f"2captcha error: {data.get('request')}")
            return None

        logger.error("2captcha task timed out")
        return None


def extract_hcaptcha_sitekey(page_html: str) -> str | None:
    """Extract the hCaptcha sitekey from a page's HTML (best effort)."""
    import re
    m = re.search(r"sitekey['\"]?\s*[:=]\s*['\"]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})['\"]", page_html, re.I)
    return m.group(1) if m else None
