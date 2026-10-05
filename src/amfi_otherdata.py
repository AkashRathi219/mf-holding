"""AMFI "Other Data" + research-information connectors.

Implements the AMFI data-source expansion plan (docs/plans/
PLAN_AMFI_DATA_SOURCES.md): the amfiindia.com pages are Next.js apps whose
tiles fetch JSON from same-origin /api/* endpoints.  Every endpoint below was
probed live on 01-Sep-2026; param formats differ per endpoint (see the date
helpers) and are documented at each fetcher.

Capabilities (jobs, each persisted atomically + telemetry via refresh_log):
  mutual-funds   MF directory + quarters + tracking months (page payloads)
  tracking       tracking error + tracking difference per MF (index/ETF QA)
  disclosure     scheme-wise disclosure of investments (SEBI circ. 25-Aug-22)
  risk-params    SEBI risk-parameter disclosure (large-cap / small-cap)
  aum            average AUM fundwise + schemewise, bifurcation, state-wise
  nfo            new fund offers (list snapshot)
  scheme-details scheme details + SSD documents + dividends (per scheme)
  selftest       one safe probe per endpoint, prints OK/FAIL

Run:  python -m src.amfi_otherdata <job> [--date DD-MMM-YYYY] [--mf MF_ID]

Storage:
  data/raw/amfi_otherdata/<job>/...     raw payloads (per fund / per month)
  data/reference/amfi_<job>.json        consolidated snapshots for the webapp
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from src.refresh_log import track

logger = logging.getLogger(__name__)

BASE_URL = "https://www.amfiindia.com"
BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "data" / "raw" / "amfi_otherdata"
REF_DIR = BASE_DIR / "data" / "reference"

PAGE_DISCLOSURE = f"{BASE_URL}/otherdata/scheme-wise-disclosure"
PAGE_TRACKING = f"{BASE_URL}/otherdata/tracking-error"

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Risk-parameter categories on /risk-parameters (strCatId).
RISK_LARGE_CAP = 17
RISK_SMALL_CAP = 18


# ---- low-level HTTP ----------------------------------------------------------

def _client(timeout: int = 60) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers=UA, follow_redirects=True,
                        verify=False)


def _get(client: httpx.Client, url: str, params: dict | None = None,
         referer: str | None = None, attempts: int = 3) -> httpx.Response:
    headers = {"Referer": referer} if referer else {}
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 * attempt)
        try:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise
            last = e
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
    raise RuntimeError(f"AMFI unreachable ({url}): {last}")


def _get_json(url: str, params: dict | None = None, referer: str | None = None,
              timeout: int = 60):
    with _client(timeout) as client:
        return _get(client, url, params=params, referer=referer).json()


def _rsc_unescape(raw: str) -> str:
    """Undo escaping inside Next.js self.__next_f.push([1,"..."]) chunks."""
    return (raw.replace('\\"', '"').replace("\\\\", "\\")
               .replace("\\u0026", "&").replace("\\/", "/"))


def _page_text(client: httpx.Client, page_url: str) -> str:
    return _rsc_unescape(_get(client, page_url).text)


def _save_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)


# ---- date helpers (AMFI uses a different format per endpoint) ----------------

_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _dd_mmm(d: date, title_case: bool = True) -> str:
    mon = _MON[d.month - 1]
    if not title_case:
        mon = mon.lower()
    return f"{d.day:02d}-{mon}-{d.year}"


def quarter_start_for(d: date | None = None) -> date:
    """Start month of the calendar quarter containing ``d`` (previous
    quarter when ``d`` falls in the first days before disclosures land)."""
    d = d or date.today()
    q_month = ((d.month - 1) // 3) * 3 + 1
    start = date(d.year, q_month, 1)
    if d.month == start.month and d.day < 10:
        # disclosures for the just-ended quarter publish ~2-3 weeks in
        prev = start - timedelta(days=1)
        q_month = ((prev.month - 1) // 3) * 3 + 1
        start = date(prev.year, q_month, 1)
    return start


def month_end(d: date | None = None) -> date:
    d = d or date.today()
    prev = d.replace(day=1) - timedelta(days=1)
    return prev.replace(day=calendar.monthrange(prev.year, prev.month)[1])


def stepback_months(start: date | None = None, tries: int = 3):
    """Yield candidate month-1st dates going back from last month."""
    d = (start or date.today()).replace(day=1)
    for _ in range(tries):
        d = d - timedelta(days=1)      # last day of the previous month
        yield d.replace(day=1)         # its 1st
        d = d.replace(day=1)           # reset so the next -1d crosses months


# ---- page-payload lookups ----------------------------------------------------

def mutual_funds(timeout: int = 60) -> list[dict]:
    """MF directory from the scheme-wise-disclosure page payload
    ([{mf_id, mf_name}], 57 funds as of 01-Sep-2026)."""
    with _client(timeout) as client:
        text = _page_text(client, PAGE_DISCLOSURE)
    pairs = re.findall(r'"mf_id":"?(\d+)"?,"mf_name":"([^"]+)"', text)
    out: dict[int, str] = {}
    for mf_id, name in pairs:
        out.setdefault(int(mf_id), name)
    return [{"mf_id": k, "mf_name": v} for k, v in sorted(out.items())]


def disclosure_quarters(timeout: int = 60) -> list[dict]:
    """Available disclosure quarters from the same page payload
    ([{QuarterName, QuarterDate ISO}], QuarterDate = quarter start)."""
    with _client(timeout) as client:
        text = _page_text(client, PAGE_DISCLOSURE)
    qs = re.findall(r'"QuarterName":"([^"]+)","QuarterDate":"([^"]+)"', text)
    # some pages emit QuarterDate before QuarterName
    qs += [(n, d) for d, n in re.findall(
        r'"QuarterDate":"([^"]+)","QuarterName":"([^"]+)"', text)]
    out: dict[str, str] = {}
    for name, iso in qs:
        out.setdefault(name, iso)
    # ISO dates sort chronologically; newest quarter last
    return [{"QuarterName": k, "QuarterDate": v}
            for k, v in sorted(out.items(), key=lambda kv: kv[1])]


def tracking_funds_and_months(timeout: int = 60) -> tuple[list[dict], list[dict]]:
    """(funds, months) from the tracking-error page payload:
    initialMutualFunds[{mfId, mfName}], initialMonthOptions[{MonthYear, Month_Date}]."""
    with _client(timeout) as client:
        text = _page_text(client, PAGE_TRACKING)
    funds = re.findall(
        r'"mfId":"?(\d+)"?,"mfName":"([^"]+)"', text)
    months = re.findall(
        r'"MonthYear":"([^"]+)","Month_Date":"([^"]+)"', text)
    f_out: dict[int, str] = {}
    for mf_id, name in funds:
        f_out.setdefault(int(mf_id), name)
    m_out: dict[str, str] = {}
    for my, md in months:
        m_out.setdefault(my, md)
    return ([{"mf_id": k, "mf_name": v} for k, v in sorted(f_out.items())],
            [{"MonthYear": k, "Month_Date": v} for k, v in m_out.items()])


# ---- fetchers (thin, one per endpoint) ---------------------------------------

def scheme_wise_disclosure(mf_id: int | str, str_month: str,
                           timeout: int = 60) -> dict:
    """SEBI 25-Aug-22 scheme-wise disclosure rows.

    ``str_month`` = quarter START as ``dd-MMM-yyyy`` Title-case (e.g.
    01-Apr-2026).  Responses: rows list, {"message":"Nil"} (AMC reports
    nothing to disclose) or {"message":"No data found."} (nothing published).
    """
    data = _get_json(f"{BASE_URL}/api/schemewisedisclosure-investment",
                     params={"MF_ID": mf_id, "strMonth": str_month},
                     referer=PAGE_DISCLOSURE, timeout=timeout)
    if isinstance(data, list):
        return {"status": "ok", "rows": data}
    msg = str((data or {}).get("message", "")).strip().lower()
    if msg == "nil":
        return {"status": "nil", "rows": []}
    return {"status": "empty", "rows": []}


def tracking_error(mf_id: int | str, strdt: str, timeout: int = 60) -> list:
    """Tracking error rows for a date.  ``strdt`` = ``dd-mmm-yyyy`` lowercase
    (e.g. 01-jul-2026; months come from tracking_months())."""
    payload = _get_json(
        f"{BASE_URL}/api/tracking-error-data",
        params={"MF_ID": mf_id, "strdt": strdt.lower()},
        referer=PAGE_TRACKING, timeout=timeout)
    return (payload or {}).get("data") or []


def tracking_difference(mf_id: int | str, dt: str, timeout: int = 60) -> list:
    """Tracking difference rows.  ``dt`` = ``DD-MMM-YYYY`` (1st of month,
    e.g. 01-Jul-2026; the API rejects other formats)."""
    payload = _get_json(
        f"{BASE_URL}/api/tracking-difference",
        params={"MF_ID": mf_id, "date": dt},
        referer=PAGE_TRACKING, timeout=timeout)
    return (payload or {}).get("data") or []


def populate_scheme(mf_id: int | str, timeout: int = 60) -> list:
    """[{scheme_id, scheme_name}] for a mutual fund."""
    return _get_json(f"{BASE_URL}/api/populate-scheme",
                     params={"MF_ID": mf_id},
                     referer=f"{BASE_URL}/otherdata/scheme-details",
                     timeout=timeout) or []


def scheme_details(mf_id: int | str, scheme_id: int | str,
                   timeout: int = 60) -> dict | None:
    """Scheme detail row (objective, load, type, category, launch date...)."""
    payload = _get_json(
        f"{BASE_URL}/api/scheme-details",
        params={"MF_ID": mf_id, "scheme_id": scheme_id},
        referer=f"{BASE_URL}/otherdata/scheme-details", timeout=timeout)
    rows = (payload or {}).get("data") or []
    return rows[0] if rows else None


def scheme_documents(scheme_id: int | str, timeout: int = 60) -> dict | None:
    """Scheme documents: infoDocumentUrl + summary PDF/XLS/XML (SSD)."""
    payload = _get_json(f"{BASE_URL}/api/schemes/{scheme_id}/documents",
                        referer=f"{BASE_URL}/otherdata/scheme-details",
                        timeout=timeout)
    rows = (payload or {}).get("data") or []
    return rows[0] if rows else None


def scheme_dividend_years(timeout: int = 60) -> list:
    return (_get_json(f"{BASE_URL}/api/years/scheme-dividend",
                      referer=f"{BASE_URL}/otherdata/scheme-dividends",
                      timeout=timeout) or {}).get("years") or []


def scheme_dividend(mf_id: int | str, scheme_id: int | str, year: str,
                    timeout: int = 60) -> list:
    """Dividend rows for a scheme+year (strSDid takes the scheme_id)."""
    payload = _get_json(
        f"{BASE_URL}/api/scheme-dividend",
        params={"MF_ID": mf_id, "strSDid": scheme_id, "strYear": year},
        referer=f"{BASE_URL}/otherdata/scheme-dividends", timeout=timeout)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    return (payload or {}).get("data") or []


def risk_parameters(cat_id: int, date_str: str, timeout: int = 60) -> list:
    """Risk-parameter rows (stress test / concentration / volatility).
    ``cat_id``: 17 = large-cap, 18 = small-cap.  ``date_str`` = 01-MMM-yyyy."""
    payload = _get_json(f"{BASE_URL}/api/risk-parameter-data-revised",
                        params={"strCatId": cat_id, "date": date_str},
                        referer=f"{BASE_URL}/risk-parameters",
                        timeout=timeout)
    if isinstance(payload, list):
        return payload
    return []


def new_fund_offers(timeout: int = 60) -> list:
    """NFO list grouped by mutual fund (each item carries Scheme_Id/MF_Id)."""
    payload = _get_json(f"{BASE_URL}/api/new-fund-offer",
                        referer=f"{BASE_URL}/new-fund-offer",
                        timeout=timeout)
    return (payload or {}).get("NewFundOffer") or []


def nfo_detail(scheme_id: int | str, timeout: int = 60) -> dict | None:
    payload = _get_json(f"{BASE_URL}/api/new-fund-offer",
                        params={"Scheme_Id": scheme_id},
                        referer=f"{BASE_URL}/new-fund-offer",
                        timeout=timeout)
    groups = (payload or {}).get("NewFundOffer") or []
    for g in groups:
        for item in g.get("items") or []:
            return item
    return None


def average_aum_fundwise(fy_id: int | None = None, period_id: int | None = None,
                         timeout: int = 60):
    """FY list (no args) -> periods (fy_id) -> per-fund AAUM table (both)."""
    params: dict = {}
    if fy_id is not None:
        params["fyId"] = fy_id
    if period_id is not None:
        params["periodId"] = period_id
    return _get_json(f"{BASE_URL}/api/average-aum-fundwise", params=params,
                     referer=f"{BASE_URL}/aum-data/average-aum",
                     timeout=timeout)


def average_aum_schemewise(str_type: str = "Categorywise",
                           fy_id: int | None = None,
                           period_id: int | None = None,
                           mf_id: int | str = 0, timeout: int = 60):
    """Scheme-level quarterly AAUM (has AMFI_Code join key).
    strType: 'Categorywise' | 'Typewise'; MF_ID=0 = all funds."""
    params: dict = {"strType": str_type, "MF_ID": mf_id}
    if fy_id is not None:
        params["fyId"] = fy_id
    if period_id is not None:
        params["periodId"] = period_id
    return _get_json(f"{BASE_URL}/api/average-aum-schemewise", params=params,
                     referer=f"{BASE_URL}/aum-data/average-aum",
                     timeout=timeout)


def statewise_aum(date_str: str, mf_id: int | str = 0,
                  timeout: int = 60) -> list:
    """Monthly AUM by state.  ``date_str`` = 01-mmm-yyyy (lowercase ok)."""
    payload = _get_json(f"{BASE_URL}/api/statewise-data",
                        params={"MF_ID": mf_id, "date": date_str},
                        referer=f"{BASE_URL}/aum-data/classified-average-aum",
                        timeout=timeout)
    return (payload or {}).get("data") or []


def scheme_catwise_aum(date_str: str, mf_id: int | str = 0,
                       timeout: int = 60) -> list:
    """Category x ticket-size table (T15/T30 buckets)."""
    payload = _get_json(f"{BASE_URL}/api/scheme-catwise-data",
                        params={"MF_ID": mf_id, "date": date_str},
                        referer=f"{BASE_URL}/aum-data/classified-average-aum",
                        timeout=timeout)
    return (payload or {}).get("data") or []


def bifurcation_aum(strdt: str, timeout: int = 60) -> list:
    """Industry AAUM under direct plan.  ``strdt`` = month-end dd-Mmm-yyyy."""
    return _get_json(f"{BASE_URL}/api/bifurcationaumdata",
                     params={"strdt": strdt},
                     referer=f"{BASE_URL}/aum-data/bifurcation-of-aum",
                     timeout=timeout)


def agewise_folio(month: str, timeout: int = 60) -> dict:
    """Investor classification.  ``month`` = Mon-YYYY (e.g. Jun-2026)."""
    return _get_json(f"{BASE_URL}/api/aum-agewise-folio-report",
                     params={"Month": month},
                     referer=f"{BASE_URL}/aum-data/age-wise-folio-data",
                     timeout=timeout)


def amc_investments(mf_id: int | str, quarter_date: str,
                    timeout: int = 60) -> list:
    """AMC/Sponsor market value in own schemes.  ``quarter_date`` =
    quarter start dd-mmm-yyyy lowercase (e.g. 01-apr-2026)."""
    payload = _get_json(f"{BASE_URL}/api/investmentscheme",
                        params={"MF_ID": mf_id, "quarterName": quarter_date},
                        referer=f"{BASE_URL}/otherdata/market-value-of-amc",
                        timeout=timeout)
    data = (payload or {}).get("data")
    if isinstance(data, dict):
        data = data.get("data") or []
    return data or []


# ---- persistence jobs --------------------------------------------------------

def job_mutual_funds(timeout: int = 90) -> dict:
    """Snapshot the MF directory + quarters + tracking months."""
    with track("amfi_otherdata_mutual_funds") as state:
        funds = mutual_funds(timeout)
        quarters = disclosure_quarters(timeout)
        t_funds, months = tracking_funds_and_months(timeout)
        doc = {
            "fetched_on": date.today().isoformat(),
            "mutual_funds": funds,
            "disclosure_quarters": quarters,
            "tracking_funds": t_funds,
            "tracking_months": months,
        }
        _save_json(REF_DIR / "amfi_mutual_funds.json", doc)
        state["funds"] = len(funds)
        state["quarters"] = len(quarters)
        state["tracking_months"] = len(months)
        return doc


def job_tracking(month_date: str | None = None, timeout: int = 60) -> dict:
    """Tracking error + difference for every MF for one month.

    ``month_date`` = DD-MMM-YYYY (1st of month); default = latest payload
    option.  Writes data/reference/amfi_tracking.json.
    """
    with track("amfi_otherdata_tracking", month=month_date or "") as state:
        funds, months = tracking_funds_and_months(timeout)
        if not funds:
            raise RuntimeError("no mutual funds in tracking page payload")
        if month_date is None:
            month_date = months[0]["Month_Date"] if months else _dd_mmm(
                date.today().replace(day=1))
        month_date = month_date.strip()
        te_rows: list[dict] = []
        td_rows: list[dict] = []
        raw_dir = RAW_DIR / "tracking" / month_date
        for i, fund in enumerate(funds):
            mf_id = fund["mf_id"]
            try:
                te = tracking_error(mf_id, month_date, timeout)
                if te:
                    for row in te:
                        row.setdefault("mfId", str(mf_id))
                        row.setdefault("mf_name", fund["mf_name"])
                    te_rows.extend(te)
            except Exception as e:  # noqa: BLE001
                logger.warning("tracking error fetch failed mf=%s: %s",
                               mf_id, e)
            try:
                td = tracking_difference(mf_id, month_date, timeout)
                if td:
                    for row in td:
                        row.setdefault("mfId", str(mf_id))
                        row.setdefault("mf_name", fund["mf_name"])
                    td_rows.extend(td)
            except Exception as e:  # noqa: BLE001
                logger.warning("tracking difference fetch failed mf=%s: %s",
                               mf_id, e)
            if (i + 1) % 10 == 0:
                time.sleep(1)  # be polite to AMFI
            _save_json(raw_dir / f"{mf_id}.json",
                       {"month": month_date, "tracking_error": te,
                        "tracking_difference": td})
        doc = {
            "fetched_on": date.today().isoformat(),
            "month": month_date,
            "funds": len(funds),
            "tracking_error": te_rows,
            "tracking_difference": td_rows,
        }
        _save_json(REF_DIR / "amfi_tracking.json", doc)
        state["te_rows"] = len(te_rows)
        state["td_rows"] = len(td_rows)
        return doc


def job_disclosure(quarter_date: str | None = None, timeout: int = 60) -> dict:
    """Scheme-wise disclosure rows for every MF for one quarter.

    ``quarter_date`` = quarter START dd-MMM-yyyy; default = latest payload
    quarter.  Writes data/reference/amfi_scheme_wise_disclosure.json.
    """
    with track("amfi_otherdata_disclosure", quarter=quarter_date or "") as state:
        funds = mutual_funds(timeout)
        if not funds:
            raise RuntimeError("no mutual funds in disclosure page payload")
        if quarter_date is None:
            quarters = disclosure_quarters(timeout)
            if quarters:
                iso = quarters[-1]["QuarterDate"]  # sorted; latest last
                d = datetime.fromisoformat(iso.replace("Z", "")).date()
                quarter_date = _dd_mmm(d)
            else:
                quarter_date = _dd_mmm(quarter_start_for())
        raw_dir = RAW_DIR / "disclosure" / quarter_date
        per_fund: list[dict] = []
        total_rows = 0
        for i, fund in enumerate(funds):
            mf_id = fund["mf_id"]
            try:
                res = scheme_wise_disclosure(mf_id, quarter_date, timeout)
            except Exception as e:  # noqa: BLE001
                logger.warning("disclosure fetch failed mf=%s: %s", mf_id, e)
                res = {"status": "error", "rows": []}
            per_fund.append({"mf_id": mf_id, "mf_name": fund["mf_name"],
                             "status": res["status"], "rows": res["rows"]})
            total_rows += len(res["rows"])
            if res["rows"]:
                _save_json(raw_dir / f"{mf_id}.json", res)
            if (i + 1) % 10 == 0:
                time.sleep(1)
        doc = {
            "fetched_on": date.today().isoformat(),
            "quarter": quarter_date,
            "funds": len(funds),
            "rows": total_rows,
            "by_fund": per_fund,
        }
        _save_json(REF_DIR / "amfi_scheme_wise_disclosure.json", doc)
        state["rows"] = total_rows
        state["with_data"] = sum(1 for f in per_fund if f["rows"])
        return doc


def job_risk_parameters(date_str: str | None = None,
                        timeout: int = 60) -> dict:
    """Risk-parameter rows for both categories (large/small cap)."""
    with track("amfi_otherdata_risk_parameters", date=date_str or "") as state:
        out: dict = {"fetched_on": date.today().isoformat(), "date": date_str}
        if date_str is None:
            # monthly disclosure; step back until a published month is found
            for cand in stepback_months(tries=4):
                rows = risk_parameters(RISK_LARGE_CAP, _dd_mmm(cand), timeout)
                if rows:
                    date_str = _dd_mmm(cand)
                    out["large_cap"] = rows
                    break
        out["date"] = date_str
        raw_dir = RAW_DIR / "risk_parameters" / (date_str or "latest")
        for label, cat in (("large_cap", RISK_LARGE_CAP),
                           ("small_cap", RISK_SMALL_CAP)):
            rows = out.get(label)
            if rows is None:
                rows = risk_parameters(cat, date_str, timeout)
            out[label] = rows
            _save_json(raw_dir / f"{label}.json", rows)
            state[label] = len(rows)
        _save_json(REF_DIR / "amfi_risk_parameters.json", out)
        return out


def job_aum(fy_id: int | None = None, period_id: int | None = None,
            timeout: int = 120) -> dict:
    """Average AUM (fundwise + schemewise both views), direct-plan
    bifurcation and state-wise AUM snapshots."""
    with track("amfi_otherdata_aum", fy=fy_id, period=period_id) as state:
        # resolve the latest FY that actually carries periods
        if fy_id is None or period_id is None:
            fys = (average_aum_fundwise(timeout=timeout) or {}).get("data") or []
            for fy in fys:
                try:
                    cand = int(fy["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                payload = average_aum_fundwise(fy_id=cand, timeout=timeout)
                periods = ((payload or {}).get("data") or {}).get(
                    "periods") or []
                if periods:
                    fy_id = fy_id if fy_id is not None else cand
                    period_id = period_id if period_id is not None else int(
                        periods[0]["id"])
                    break
        fundwise = average_aum_fundwise(fy_id=fy_id, period_id=period_id,
                                        timeout=timeout)

        schemewise: dict = {}
        for str_type in ("Categorywise", "Typewise"):
            try:
                schemewise[str_type.lower()] = average_aum_schemewise(
                    str_type=str_type, fy_id=fy_id, period_id=period_id,
                    timeout=timeout)
            except Exception as e:  # noqa: BLE001
                logger.warning("schemewise AUM (%s) failed: %s", str_type, e)

        # Aug-style months can publish late; step back until data appears
        bifurcation: list = []
        statewise: list = []
        for cand in stepback_months(tries=4):
            if not bifurcation:
                try:
                    bifurcation = bifurcation_aum(_dd_mmm(
                        cand.replace(day=calendar.monthrange(
                            cand.year, cand.month)[1])), timeout=timeout)
                except Exception as e:  # noqa: BLE001
                    logger.warning("bifurcation %s failed: %s", cand, e)
            if not statewise:
                try:
                    statewise = statewise_aum(_dd_mmm(cand,
                                                      title_case=False),
                                              timeout=timeout)
                except Exception as e:  # noqa: BLE001
                    logger.warning("statewise %s failed: %s", cand, e)
            if bifurcation and statewise:
                break

        doc = {
            "fetched_on": date.today().isoformat(),
            "fy_id": fy_id,
            "period_id": period_id,
            "fundwise": fundwise,
            "schemewise": schemewise,
            "bifurcation_direct_plan": bifurcation,
            "statewise": statewise,
        }
        _save_json(REF_DIR / "amfi_average_aum.json", doc)
        state["fundwise_rows"] = len((fundwise or {}).get("data") or [])
        state["statewise_rows"] = len(statewise)
        return doc


def job_nfo(timeout: int = 90) -> dict:
    """New-fund-offer list snapshot."""
    with track("amfi_otherdata_nfo") as state:
        groups = new_fund_offers(timeout)
        doc = {"fetched_on": date.today().isoformat(), "groups": groups}
        _save_json(REF_DIR / "amfi_nfo.json", doc)
        state["nfo_schemes"] = sum(len(g.get("items") or []) for g in groups)
        return doc


def job_scheme_details(mf_id: int | str, scheme_id: int | str,
                       timeout: int = 60) -> dict:
    """Fetch + persist scheme details, SSD documents and dividends for one
    scheme (manual/bulk use; the bulk harvest is a later-phase job)."""
    with track("amfi_otherdata_scheme_details", mf=mf_id,
               scheme=scheme_id) as state:
        details = scheme_details(mf_id, scheme_id, timeout)
        docs = scheme_documents(scheme_id, timeout)
        dividends = scheme_dividend(mf_id, scheme_id, str(date.today().year),
                                    timeout)
        doc = {"fetched_on": date.today().isoformat(), "mf_id": mf_id,
               "scheme_id": scheme_id, "details": details, "documents": docs,
               "dividends": dividends}
        raw_dir = RAW_DIR / "scheme_details" / str(mf_id)
        _save_json(raw_dir / f"{scheme_id}.json", doc)
        state["has_details"] = bool(details)
        return doc


# ---- selftest ----------------------------------------------------------------

def selftest(timeout: int = 60) -> dict:
    """One safe probe per endpoint; prints and returns a status table."""
    results: list[tuple[str, str, str]] = []

    def check(name: str, fn):
        try:
            out = fn()
            size = len(out) if hasattr(out, "__len__") else 1
            results.append((name, "OK", f"len={size}"))
        except Exception as e:  # noqa: BLE001
            results.append((name, "FAIL", str(e)[:120]))

    funds, months = tracking_funds_and_months(timeout)
    mf = funds[0]["mf_id"] if funds else 20
    month = months[0]["Month_Date"] if months else "01-Jul-2026"
    quarter = _dd_mmm(quarter_start_for())

    check("mutual_funds", lambda: mutual_funds(timeout))
    check("disclosure_quarters", lambda: disclosure_quarters(timeout))
    check("scheme_wise_disclosure",
          lambda: scheme_wise_disclosure(mf, quarter, timeout))
    check("tracking_error", lambda: tracking_error(mf, month, timeout))
    check("tracking_difference",
          lambda: tracking_difference(mf, month, timeout))
    check("populate_scheme", lambda: populate_scheme(mf, timeout))
    check("risk_parameters",
          lambda: risk_parameters(RISK_LARGE_CAP,
                                  _dd_mmm(month_end().replace(day=1)),
                                  timeout))
    check("new_fund_offers", lambda: new_fund_offers(timeout))
    check("average_aum_fundwise", lambda: average_aum_fundwise(
        timeout=timeout))
    check("average_aum_schemewise",
          lambda: average_aum_schemewise(fy_id=2, period_id=1, timeout=timeout))
    check("statewise_aum",
          lambda: statewise_aum(_dd_mmm(month_end().replace(day=1),
                                        title_case=False), timeout=timeout))
    check("bifurcation_aum", lambda: bifurcation_aum(_dd_mmm(month_end()),
                                                     timeout))
    check("agewise_folio",
          lambda: agewise_folio(f"{_MON[month_end().month - 1]}-"
                                f"{month_end().year}", timeout))

    print(f"{'endpoint':<26}{'status':<8}detail")
    for name, status, detail in results:
        print(f"{name:<26}{status:<8}{detail}")
    return dict((n, (s, d)) for n, s, d in results)


# ---- CLI ---------------------------------------------------------------------

MONTHLY_JOBS = ("mutual-funds", "tracking", "disclosure", "risk-params",
                "aum", "nfo")


def run_monthly_all(timeout: int = 120) -> dict:
    """Scheduler entry: run the full monthly set, per-job guarded.

    Never raises; returns {job: 'ok' | 'failed: ...'} for telemetry."""
    summary: dict = {}
    jobs = {
        "mutual-funds": lambda: job_mutual_funds(timeout),
        "tracking": lambda: job_tracking(timeout=timeout),
        "disclosure": lambda: job_disclosure(timeout=timeout),
        "risk-params": lambda: job_risk_parameters(timeout=timeout),
        "aum": lambda: job_aum(timeout=timeout),
        "nfo": lambda: job_nfo(timeout),
    }
    for name, fn in jobs.items():
        try:
            fn()
            summary[name] = "ok"
        except Exception as e:  # noqa: BLE001  (telemetry must not die)
            logger.warning("amfi-otherdata %s failed: %s", name, e)
            summary[name] = f"failed: {str(e)[:200]}"
    return summary


JOBS = ("mutual-funds", "tracking", "disclosure", "risk-params", "aum", "nfo",
        "scheme-details", "selftest")


def run_jobs(jobs: list[str], **opts) -> None:
    for job in jobs:
        logger.info("amfi-otherdata job: %s", job)
        if job == "mutual-funds":
            job_mutual_funds(opts.get("timeout", 90))
        elif job == "tracking":
            job_tracking(opts.get("date"), opts.get("timeout", 60))
        elif job == "disclosure":
            job_disclosure(opts.get("date"), opts.get("timeout", 60))
        elif job == "risk-params":
            job_risk_parameters(opts.get("date"), opts.get("timeout", 60))
        elif job == "aum":
            job_aum(opts.get("fy"), opts.get("period"),
                    opts.get("timeout", 120))
        elif job == "nfo":
            job_nfo(opts.get("timeout", 90))
        elif job == "scheme-details":
            job_scheme_details(opts["mf"], opts["scheme"],
                               opts.get("timeout", 60))
        elif job == "selftest":
            selftest(opts.get("timeout", 60))
        else:
            raise SystemExit(f"unknown job: {job} (choose from {JOBS})")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    ap = argparse.ArgumentParser(description="AMFI other-data connectors")
    ap.add_argument("job", choices=JOBS)
    ap.add_argument("--date", "-d", default=None,
                    help="DD-MMM-YYYY (tracking/disclosure/risk-params)")
    ap.add_argument("--mf", type=int, default=None, help="MF_ID (scheme-details)")
    ap.add_argument("--scheme", type=int, default=None,
                    help="scheme_id (scheme-details)")
    ap.add_argument("--fy", type=int, default=None, help="AMFI financial-year id")
    ap.add_argument("--period", type=int, default=None, help="AMFI period id")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args(argv)

    run_jobs([args.job], date=args.date, mf=args.mf, scheme=args.scheme,
             fy=args.fy, period=args.period, timeout=args.timeout)


if __name__ == "__main__":
    main()
