"""Official AMFI portal integration.

Two independent capabilities, both sourced straight from amfiindia.com:

1. Portfolio-disclosure directory (https://www.amfiindia.com/online-center/
   portfolio-disclosure): AMFI does NOT host consolidated portfolio files --
   the page embeds an official per-AMC directory of disclosure-page URLs
   (`members[]` RSC payload). We scrape it to verify/fill the local AMC
   registry (`config/amc_registry.json`), which drives the existing
   download+parse pipeline. Holdings ingested via those pages land as
   source='amc_website', which already outranks advisorkhoj in
   ``webapp.db._SOURCE_PRIORITY``.

2. SIF latest NAV (https://www.amfiindia.com/sif/latest-nav): public JSON API
   behind the page -> /api/sif-latest-nav returning scheme rows with
   Sd_Id / NavName / Plan / Option / ISINPO / ISINRI / NetAssetValue / Date.

Everything here is best-effort tolerant: network helpers raise, but callers
(jobs) wrap them so telemetry never breaks the pipeline.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = BASE_DIR / "config" / "amc_registry.json"
MEMBERS_CACHE = BASE_DIR / "data" / "reference" / "amfi_disclosure_members.json"
SIF_NAV_PATH = BASE_DIR / "data" / "parsed" / "sif" / "sif_latest_nav.json"

PORTAL_DISCLOSURE_URL = "https://www.amfiindia.com/online-center/portfolio-disclosure"
SIF_LATEST_NAV_API = "https://www.amfiindia.com/api/sif-latest-nav"
SIF_TYPES = ["", "Open Ended", "Close Ended", "Interval Fund"]

UA = {"User-Agent": "FactsheetEngineAI/0.1 (data research; contact via github)"}


def _norm_amc(name: str) -> str:
    """Loose AMC-name normalizer for matching portal names to the registry."""
    s = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    for token in ("mutualfund", "assetmanagement", "amc", "limited", "ltd"):
        s = s.replace(token, "")
    return s


def _get(client: httpx.Client, url: str, attempts: int = 3,
         params: dict | None = None) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 * attempt)
        try:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise
            last = e
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
    raise RuntimeError(f"AMFI unreachable ({url}): {last}")


# ---- 1. portfolio-disclosure members directory ------------------------------

def _rsc_unescape(raw: str) -> str:
    """Undo the escaping used inside self.__next_f.push([1,"..."]) chunks."""
    out = raw.replace('\\"', '"').replace("\\\\", "\\")
    out = out.replace("\\u0026", "&").replace("\\/", "/")
    return out


def scrape_members(timeout: int = 60) -> list[dict]:
    """Scrape the official members directory from the portal page.

    Returns [{mf_id, mf_name, amc_name, monthly_url, fortnightly_url,
    half_yearly_url}] (URLs may be '')."""
    with httpx.Client(timeout=timeout, headers=UA, follow_redirects=True) as client:
        html = _get(client, PORTAL_DISCLOSURE_URL).text
    text = _rsc_unescape(html)
    pat = re.compile(
        r'"mf_id":"(?P<id>\d+)","mf_name":"(?P<mf>[^"]+)"'
        r'(?:,"amc_name":"(?P<amc>[^"]*)")?'
        r'.*?"amc_fortnightly_portfolio_disclosure":"(?P<fortnight>[^"]*)"'
        r',"amc_monthly_portfolio_disclosure":"(?P<monthly>[^"]*)"'
        r'(?:,"amc_halfYearly_portfolio_disclosure":"(?P<halfyear>[^"]*)")?',
        re.S)
    seen: dict[str, dict] = {}
    for m in pat.finditer(text):
        g = m.groupdict()
        seen[g["mf"]] = {
            "mf_id": g["id"],
            "mf_name": g["mf"],
            "amc_name": g.get("amc") or "",
            "monthly_url": g["monthly"] or "",
            "fortnightly_url": g["fortnight"] or "",
            "half_yearly_url": g.get("halfyear") or "",
        }
    return list(seen.values())


def refresh_registry(members: list[dict], dry_run: bool = False) -> dict:
    """Verify/fill config/amc_registry.json monthly-disclosure URLs.

    Registry wins unless its entry is empty (fill) -- a live AMFI URL never
    silently overwrites a curated one; differences are reported instead.
    Returns {added, updated, verified_same, unmatched_portal, still_empty}."""
    reg_raw = REGISTRY_PATH.read_text(encoding="utf-8-sig")
    registry = json.loads(reg_raw)

    by_norm = {}
    for entry in registry:
        by_norm[_norm_amc(entry.get("mf_name") or "")] = entry

    added = updated = same = 0
    mutated = False
    still_empty: list[str] = []
    unmatched: list[str] = []
    changes: list[tuple[str, str, str]] = []  # (mf_name, old, new)
    today = date.today().isoformat()
    for mem in members:
        entry = by_norm.get(_norm_amc(mem["mf_name"]))
        if entry is None:
            unmatched.append(mem["mf_name"])
            continue
        url = mem["monthly_url"]
        cur = entry.get("amc_monthly_portfolio_disclosure") or ""
        if not url:
            if not cur:
                still_empty.append(mem["mf_name"])
            continue
        if not cur:
            entry["amc_monthly_portfolio_disclosure"] = url
            entry["amfi_verified"] = today
            added += 1
            mutated = True
            changes.append((mem["mf_name"], "", url))
        elif cur.rstrip("/") != url.rstrip("/"):
            # report-only: curated URLs stay authoritative until manually blessed
            changes.append((mem["mf_name"], cur, url))
            if entry.get("amfi_directory_url") != url:
                entry["amfi_directory_url"] = url
                mutated = True
            updated += 1
        else:
            same += 1

    report = {
        "portal_members": len(members),
        "verified_same": same,
        "filled_empty": added,
        "differs_reported": updated,
        "unmatched_portal": sorted(unmatched),
        "still_empty": sorted(set(still_empty)),
        "changes": [{"mf_name": n, "registry": o, "amfi": u} for n, o, u in changes],
    }
    if not dry_run and mutated:
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
    MEMBERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MEMBERS_CACHE.write_text(json.dumps(
        {"fetched_on": today, "members": members}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return report


# ---- 2. SIF latest NAV -------------------------------------------------------

def fetch_sif_nav(sif_type: str = "", timeout: int = 60) -> dict | list:
    """Call AMFI's public SIF latest-NAV API. Empty type returns everything."""
    params = {"type": sif_type}
    with httpx.Client(timeout=timeout, headers=UA, follow_redirects=True) as client:
        return _get(client, SIF_LATEST_NAV_API, params=params).json()


def flatten_sif_rows(payload: dict | list) -> list[dict]:
    """Normalize the API's two response shapes into flat row dicts."""
    rows: list[dict] = []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data = data.get("data") or []
    for item in data or []:
        if isinstance(item, dict) and item.get("categories"):
            for cat in item["categories"]:
                for grp in cat.get("groups") or []:
                    for r in grp.get("schemes") or []:
                        rows.append(_sif_row(r))
        elif isinstance(item, dict):
            rows.append(_sif_row(item))
    return rows


def _sif_row(r: dict) -> dict:
    return {
        "sif_code": r.get("Sd_Id"),
        "nav_name": r.get("NavName"),
        "plan": r.get("Plan"),
        "option": r.get("Option"),
        "isin_payout_growth": (r.get("ISINPO") or "").strip() or None,
        "isin_reinvestment": (r.get("ISINRI") or "").strip() or None,
        "nav": r.get("NetAssetValue"),
        "date": r.get("Date"),
    }


def save_sif_nav(timeout: int = 90) -> dict:
    """Fetch every disclosure type, dedupe rows, persist one snapshot."""
    all_rows: dict[tuple, dict] = {}
    for t in SIF_TYPES:
        try:
            payload = fetch_sif_nav(sif_type=t, timeout=timeout)
        except Exception:
            continue  # one failing tab must not kill the snapshot
        for r in flatten_sif_rows(payload):
            key = (r.get("sif_code"), r.get("nav_name"), r.get("plan"),
                   r.get("option"))
            all_rows.setdefault(key, r)
    rows = sorted(all_rows.values(),
                  key=lambda r: (str(r.get("nav_name") or ""), str(r.get("plan") or "")))
    doc = {"fetched_on": date.today().isoformat(), "count": len(rows), "rows": rows}
    SIF_NAV_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SIF_NAV_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SIF_NAV_PATH)
    try:
        from .remote_store import is_configured, upload_object
        if is_configured():
            upload_object("parsed/sif/" + SIF_NAV_PATH.name, SIF_NAV_PATH)
    except Exception:
        pass
    return doc
