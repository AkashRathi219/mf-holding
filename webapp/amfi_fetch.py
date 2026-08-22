"""Fetch mutual-fund monthly portfolio holdings (with %NAV) from an external
provider and write them in the normalized AMFI schema consumed by the webapp.

Primary source: mfdata.in (free, open, no-auth) — returns per-scheme holdings
with ``weight_pct`` (the SEBI/AMFI monthly-disclosure fields). A provider class
can be swapped for an AMFI endpoint later.

Output schema (data/parsed/amfi/amfi_<month>.json):
    { "amc": ..., "as_of": "YYYY-MM-DD",
      "schemes": { "<fund name>": { "holdings": [
          {"company","isin","percent_nav","market_value","sector","section"} ] } } }
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
AMFI_DIR = BASE_DIR / "data" / "parsed" / "amfi"
MFDATA_BASE = "https://mfdata.in/api/v1"

UA = {"User-Agent": "FactsheetEngineAI/0.1 (data research)"}


class ProviderDown(Exception):
    """mfdata.in unreachable or answering 5xx — retried, then surfaced."""


def _get_json(client: httpx.Client, url: str, attempts: int = 3) -> dict | list:
    """GET JSON with backoff on 5xx/transport errors; raises ProviderDown with
    a human-readable reason when the provider is genuinely unavailable."""
    last_err: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 * attempt)  # 0s, 2s, 4s
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code < 500:
                raise  # 4xx: real request problem, don't retry
            last_err = e
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_err = e
    detail = ""
    if isinstance(last_err, httpx.HTTPStatusError):
        detail = f"HTTP {last_err.response.status_code}"
        if last_err.response.status_code == 522:
            detail += " (Cloudflare: provider origin server is down)"
    elif last_err is not None:
        detail = type(last_err).__name__
    raise ProviderDown(f"mfdata.in unavailable — {detail}. "
                       f"Will auto-retry on the next scheduled run.")


def _weight(rec: dict, key: str = "weight_pct") -> float | None:
    v = rec.get(key) or rec.get("percent_nav") or rec.get("pct_nav")
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if 0 < f < 1:
        return round(f * 100, 6)  # fraction -> percent
    return round(f, 6)


def fetch_mfdata(month: str | None = None, timeout: int = 60) -> list[dict]:
    """Fetch all AMC families' holdings from mfdata.in and normalize them.

    Returns a list of per-AMC dicts in the AMFI output schema. Raises on network
    or provider errors (caller decides whether to fail hard).
    """
    with httpx.Client(timeout=timeout, headers=UA, follow_redirects=True) as client:
        families = _get_json(client, f"{MFDATA_BASE}/families")
        families = families if isinstance(families, list) else families.get("data", [])

        out: list[dict] = []
        for fam in families:
            fid = fam.get("id") or fam.get("family_id")
            name = fam.get("name") or fam.get("family_name") or str(fid)
            url = f"{MFDATA_BASE}/families/{fid}/holdings"
            if month:
                url += f"?month={month}"
            try:
                data = _get_json(client, url)
                holdings = data.get("data") or data
            except ProviderDown:
                raise  # provider-wide outage: abort fast, caller logs it
            except Exception:
                continue  # skip a family that fails; keep the rest
            schemes: dict[str, dict] = {}
            for h in holdings.get("equity_holdings", []) + holdings.get("debt_holdings", []):
                scheme = (h.get("scheme_name") or h.get("scheme") or h.get("fund_name") or "").strip()
                if not scheme:
                    continue
                pct = _weight(h)
                schemes.setdefault(scheme, {"holdings": []})
                schemes[scheme]["holdings"].append({
                    "company": (h.get("stock_name") or h.get("company") or "").strip(),
                    "isin": (h.get("isin") or "").strip().upper(),
                    "percent_nav": pct,
                    "market_value": h.get("market_value") or h.get("value"),
                    "sector": (h.get("sector") or "").strip(),
                    "section": (h.get("instrument_type") or h.get("asset_class") or "").strip(),
                })
            if schemes:
                out.append({"amc": name, "as_of": month or "", "schemes": schemes})
        return out


def save(out: list[dict], month: str) -> list[Path]:
    """Write one file per AMC in the schema the webapp reader expects
    (``{amc, as_of, schemes}`` per file — ``webapp/db._load_amfi_schemes``
    iterates data/parsed/amfi/*.json as individual dicts)."""
    import re
    AMFI_DIR.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9A-Za-z_-]+", "-", month or "latest").strip("-") or "latest"
    paths: list[Path] = []
    for rec in out:
        slug = re.sub(r"[^a-z0-9]+", "_", str(rec.get("amc") or "").lower()).strip("_") or "amc"
        p = AMFI_DIR / f"{stamp}_{slug}.json"
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        paths.append(p)
    return paths


def main() -> int:
    import click

    @click.command()
    @click.option("--month", "-m", default=None, help="YYYY-MM of the disclosure (default: latest)")
    @click.option("--timeout", type=int, default=60)
    def _cli(month, timeout):
        try:
            data = fetch_mfdata(month=month, timeout=timeout)
        except Exception as e:
            click.echo(f"ERROR: provider unreachable: {e}", err=True)
            click.echo("Hint: mfdata.in may be down; retry later or point at another provider.", err=True)
            raise SystemExit(1)
        if not data:
            click.echo("No scheme holdings returned by the provider.")
            return 1
        paths = save(data, month or "latest")
        schemes = sum(len(d["schemes"]) for d in data)
        click.echo(f"Saved {len(data)} AMCs / {schemes} schemes -> {len(paths)} files in {AMFI_DIR}")
        return 0

    return _cli()


if __name__ == "__main__":
    sys.exit(main())