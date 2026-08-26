"""AMFI TER download + universe mapping.

Downloads the Total Expense Ratio (TER) disclosures for a given month from
AMFI's "TER of MF Schemes" page, consolidates the per-day rows to scheme
level, and maps the scheme universe (the Combined NAV file) onto the TER data
so we can report which schemes are missing a TER for the month.

Endpoints (all GET, base https://www.amfiindia.com):
  /api/populate-ter-month?year=YYYY-YYYY        available months
  /api/populate-te-rdata-revised?MF_ID=&Month=&strCat=&strType=[&excel=true]
                                                TER data (JSON or Excel)

Note: the ``excel=true`` export returns the *entire* monthly file regardless
of the ``strCat`` / ``strType`` filters (verified 2026-08-20: every
fund-type x category combination yields a byte-identical 63,157-row file
covering Open Ended + Interval Fund + Close Ended).  A single download
therefore covers all combinations; the consolidated output is split by
``Scheme Type`` afterwards.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from src.amfi_nav import fund_name_from_nav

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # [slim-deps] pandas stays out of the runtime/boot graph
    import pandas as pd

BASE_URL = "https://www.amfiindia.com"
TER_DATA_PATH = "/api/populate-te-rdata-revised"
TER_MONTH_PATH = "/api/populate-ter-month"
REFERER = f"{BASE_URL}/ter-of-mf-schemes"

# AMFI fund-type filter labels on the TER page.
FUND_TYPES = ["Open Ended", "Interval Fund", "Close Ended"]

# Excel column names (scheme-level, one row per NSDL scheme code per day).
COL_CODE = "NSDL Scheme Code"
COL_NAME = "Scheme Name"
COL_TYPE = "Scheme Type"
COL_CAT = "Scheme Category"
COL_DATE = "TER Date"
COL_R = "Regular Plan - Total TER (%)"
COL_D = "Direct Plan - Total TER (%)"


def _key(s: str) -> str:
    """Lookup key for a scheme name: lowercase, stripped of plan/option
    clutter, hyphens/punctuation collapsed.  Mirrors the spirit of
    ``amfi_nav.norm`` but also drops parenthetical notes and the trailing
    ``Fund`` word so ``Kotak Business Cycle`` matches ``Kotak Business Cycle
    Fund``."""
    s = (str(s) or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"formerly known as.*", " ", s)
    s = re.sub(r"\bthe\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+fund(\s|$)", " ", s)
    return s.strip()


def _has_keys() -> httpx.Headers | dict[str, str]:
    return {
        "Referer": REFERER,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
    }


def _resolve_fund_name(nav_name, display_name: str) -> str:
    """Fund-level name for a universe row.

    Prefers the NAVAll-derived name (authoritative, clean suffix stripping).
    Falls back to the universe display name when NAVAll lacks the scheme
    (matured/wound-up), and guards against NAVAll over-trimming (e.g.
    ``UTI-Dividend Yield Fund.-Growth`` -> ``UTI``) by switching to the
    display name when the NAVAll result is implausibly short.
    """
    nav_fund = fund_name_from_nav(nav_name) if isinstance(nav_name, str) and nav_name else ""
    disp_fund = fund_name_from_nav(display_name)
    if not nav_fund:
        return disp_fund
    if len(nav_fund) < 6 and len(disp_fund) > len(nav_fund):
        return disp_fund
    return nav_fund


def available_months(year: str = "2026-2027") -> list[dict]:
    """List months AMFI has TER data for a financial year."""
    r = httpx.get(
        f"{BASE_URL}{TER_MONTH_PATH}",
        params={"year": year},
        headers=_has_keys(),
        verify=False,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def download_ter_excel(month: str, year: str, out_dir: Path) -> Path:
    """Download the TER Excel export for a month (covers all fund types).

    ``month`` is the AMFI ``MM-YYYY`` value (e.g. ``07-2026``).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"TER_{month}.xlsx"
    if out_path.exists() and out_path.stat().st_size > 0:
        logger.info("TER export already downloaded: %s", out_path)
        return out_path
    r = httpx.get(
        f"{BASE_URL}{TER_DATA_PATH}",
        params={
            "MF_ID": "All",
            "Month": month,
            "strCat": "-1",
            "strType": "-1",
            "excel": "true",
        },
        headers=_has_keys(),
        verify=False,
        timeout=240,
    )
    r.raise_for_status()
    out_path.write_bytes(r.content)
    logger.info("Downloaded TER export: %s (%d bytes)", out_path, len(r.content))
    return out_path


def load_ter_schemes(xlsx_path: Path) -> pd.DataFrame:
    """Read the raw export and reduce to one row per NSDL scheme code.

    AMFI publishes the same TER for every day of the disclosure month, so we
    keep the last available day for each scheme.
    """
    # [slim-deps] pandas is only needed by the TER CLI/mapping pipeline, never
    # at server boot; import lazily so importing this module stays slim-safe.
    import pandas as pd

    df = pd.read_excel(xlsx_path)
    df.columns = [c.strip() for c in df.columns]
    df[COL_DATE] = pd.to_datetime(df[COL_DATE])
    df = df.sort_values(COL_DATE).groupby(COL_CODE, as_index=False).tail(1)
    df[COL_R] = pd.to_numeric(df[COL_R], errors="coerce")
    df[COL_D] = pd.to_numeric(df[COL_D], errors="coerce")
    df["_key"] = df[COL_NAME].apply(_key)
    return df


def load_universe(csv_path: Path) -> pd.DataFrame:
    """Load the Combined NAV universe file (fund+plan rows)."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    return df


def load_navall_names(navall_path: Path) -> dict[str, str]:
    """Map AMFI scheme code -> full scheme name from the NAVAll feed."""
    mapping: dict[str, str] = {}
    with open(navall_path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(";")
            if len(parts) == 6 and parts[0].strip().isdigit():
                mapping[parts[0].strip()] = parts[3]
    return mapping


# Curated aliases for schemes that are present in the July-2026 TER file under
# a materially different spelling than the universe / NAVAll name.  Keys are
# the universe fund-level name (as computed from NAVAll), values the exact
# scheme name in the TER export.  Verified against the 2026-07 export.
_ALIASES: dict[str, str] = {
    "ADITYA BIRLA SUNLIFE OVERNIGHT FUND": "ADITYA BIRLA SUN LIFE OVERNIGHT FUND",
    "Aditya Birla SL Banking&PSU Debt Fund": "Aditya Birla Sun Life Banking & PSU Debt Fund",
    "Aditya Birla SL Liquid Fund": "Aditya Birla Sun Life Liquid Fund",
    "Aditya Birla Sun Life Crisil IBX Gilt Apr 2033 Index Fund": "Aditya Birla Sun Life Crisil IBX Gilt April 2033 Index Fund",
    "Aditya Birla Sun Life Savings Fund - Discipline Advantage Plan": "Aditya Birla Sun Life Savings Fund",
    "Aditya Birla Sun Life US Treasury 1-3 Year Bond ETFs Fund Of Funds": "Aditya Birla Sun Life US Treasury 1-3 Year Bond ETFs Passive FOF",
    "Aditya Birla Sun Life US Treasury 3-10 Year Bond ETFs Fund Of Funds": "Aditya Birla Sun Life US Treasury 3-10 Year Bond ETFs Passive FOF",
    "Aditya Birla Sunlife Nifty Next 50 ETF": "Aditya Birla Sun Life Nifty Next 50 ETF",
    "Axis BSE India Sectors Leaders Index Fund": "Axis BSE India Sector Leaders Index Fund",
    "Axis Children's Fund": "Axis Children's Fund",
    "Axis IT ETF": "Axis NIFTY IT ETF",
    "Axis Retirement Fund": "Axis Retirement Fund - Dynamic Plan",
    "Aditya Birla SL Banking&PSU Debt Fund-Ret(G)": "Aditya Birla Sun Life Banking & PSU Debt Fund",
    "BANDHAN CRISIL IBX 90:10 SDL PLUS GILT - SEP27 INDEX FUND": "BANDHAN CRISIL IBX 90:10 SDL PLUS GILT - SEPTEMBER 2027 INDEX FUND",
    "BANDHAN CRISIL IBX 90:10 SDL PLUS GILT SEP27 INDEX FUND": "BANDHAN CRISIL IBX 90:10 SDL PLUS GILT - SEPTEMBER 2027 INDEX FUND",
    "Baroda BNP Paribas Best-in-Class Strategy Fund": "Baroda BNP Paribas ESG Best-in-Class Strategy Fund",
    "Baroda BNP Paribas Income Plus Arbitrage Active FOF": "Baroda BNP Paribas Income Plus Arbitrage Active Fund of Funds",
    "CANARA ROBECO FLEXICAP FUND": "Canara Robeco Flexi Cap Fund",
    "Canara Robeco Banking and Financials Services Fund": "Canara Robeco Banking and Financial Services Fund",
    "Edelweiss CRISIL IBX AAA Financial Services - Jan 2028 Index Fund": "Edelweiss CRISIL IBX AAA Financial Services Bond - Jan 2028 Index Fund",
    "Edelweiss Income Plus Arbitrage Active Fund of Funds": "Edelweiss Income Plus Arbitrage Omni Fund of Funds",
    "Edelweiss Nifty LargeMidcap 250 Plus 8-13 yr G-Sec 70-30 Index Fund": "Edelweiss Nifty LargeMidcap250 Plus 8-13yr G-Sec 70:30 Index Fund",
    "Franklin India INDEX FUND- NSE NIFTY 50 INDEX FUND": "Franklin India NSE Nifty 50 Index Fund",
    "Franklin India Index Fund- NSE Nifty 50 Index Fund": "Franklin India NSE Nifty 50 Index Fund",
    "Groww Largecap Fund": "Groww Large Cap Fund",
    "Groww Nifty Smallcap250 ETF": "Groww Nifty Smallcap 250 ETF",
    "HDFC BSE India Sector Leaders India Fund": "HDFC BSE India Sector Leaders Index Fund",
    "HDFC Childrens Fund": "HDFC Children's Fund",
    "HDFC Multi-Asset Fund": "HDFC Multi-Asset Allocation Fund",
    "HDFC NIFTY 50 VALUE 20 ETF": "HDFC Nifty50 Value 20 ETF",
    "HDFC NIFTY Reality Index Fund": "HDFC NIFTY REALTY INDEX FUND",
    "ITI Large & Midcap Fund": "ITI Large & Mid Cap Fund",
    "Kotak Gilt-Investment Provident Fund and Trust": "Kotak Gilt Fund",
    "Kotak Gilt-Investment Regular": "Kotak Gilt Fund",
    "Kotak Gold Fund Growth": "Kotak Gold Fund",
    "Mahindra Manulife Banking & Financial Services Fund": "MahindraManulife Banking & Financial Services Fund",
    "Mirae Asset Nifty 50 Equal Weight ETF": "Mirae Asset Nifty50 Equal Weight ETF",
    "Mirae Asset Nifty 8-13 yr Gsec ETF": "Mirae Asset Nifty 8-13 yr G-Sec ETF",
    "Mirae AssetHang Seng TECH ETF Fund of Fund": "Mirae Asset Hang Seng TECH ETF Fund of Fund",
    "Motilal Oswal Asset Allocation FOF- C": "Motilal Oswal Asset Allocation Fund of Fund- Conservative",
    "Motilal Oswal Developed Market Ex US ETFs Fund of Funds": "Motilal Oswal Developed Market Ex US ETFs Overseas Equity Passive FOF",
    "Motilal Oswal Nifty Midcap150 Momentum 50 ETF": "Motilal Oswal Nifty Midcap 150 Momentum 50 ETF",
    "Motilal Owsal Manufacturing Fund": "Motilal Oswal Manufacturing Fund",
    "Navi ELSS Tax Saver Fund": "Navi ELSS Tax Saver Nifty 50 Index Fund",
    "Navi Nifty Midsmall 400 Index Fund": "Navi Nifty Midsmallcap 400 Index Fund",
    "Navi Nifty Smallcap 250 Momentum quality index fund": "Navi Nifty Smallcap250 Momentum Quality 100 Index Fund",
    "Navi NiftyIT Index Fund": "Navi Nifty IT Index Fund",
    "Nippon India Retirement Fund": "Nippon India Retirement Fund- Wealth Creation Scheme",
    "SBI Children's Fund": "SBI Children's Fund - Savings Plan",
    "SBI Retirement Benefit Fund": "SBI Retirement Benefit Fund - Aggressive Plan",
    "SBI SHORT HORIZON DEBT FUND-SHORT TERM FUND": "SBI SHORT TERM DEBT FUND",
    "TRUST MF OVERNIGHT FUND": "TRUSTMF Overnight Fund",
    "Tata Nifty SDL Plus AAA PSU Bond Dec 6040 Index Fund": "TATA NIFTY SDL PLUS AAA PSU BOND DEC 2027 60 40 INDEX FUND",
    "Tata S&P BSE Sensex Index Fund": "TATA BSE SENSEX INDEX FUND",
    "UTI MMF": "UTI - Money Market Fund",
    "WhiteOak Capital Pharma and Heathcare Fund": "WhiteOak Capital Pharma and Healthcare Fund",
}

# Schemes that are legitimately absent from the July-2026 TER file (matured
# before the month, wound up, or AMCs with no TER disclosure).  Kept so the
# missing report can explain *why* rather than just list names.
MISSING_REASON = {
    "AXIS Nifty AAA Bond Plus SDL Apr 2026 50:50 ETF": "matured Apr-2026",
    "AXIS Nifty AAA Bond Plus SDL Apr 2026 50:50 ETF FOF": "matured Apr-2026",
    "AXIS Nifty AAA Bond Plus SDL Apr 2026 50:50 ETF FOF-Dir": "matured Apr-2026",
    "Abakkus Large & Mid Cap Fund": "no TER disclosure",
    "Aditya Birla Sun Life Crisil IBX 60:40 SDL + AAA PSU APR 2026 Index Fund": "matured Apr-2026",
    "Aditya Birla Sun Life Crisil IBX 60:40 SDL+AAA PSU APR 2026 Index Fund": "matured Apr-2026",
    "Aditya Birla Sun Life Crisil IBX Gilt-April 2026 Index Fund": "matured Apr-2026",
    "AlphaGrep Flexi Cap Fund": "no TER disclosure",
    "AlphaGrep Liquid Omni FoF": "no TER disclosure",
    "Axis Income Plus Arbitrage Omni FOF": "not in TER export",
    "Axis Multi-Asset Omni FoF": "not in TER export",
    "BANDHAN CRISIL IBX GILT APRIL 2026 INDEX FUND": "matured Apr-2026",
    "Baroda BNP Paribas Services Fund": "not in TER export",
    "DSP BSE MidSmall Private Banks ETF": "not in TER export",
    "Edelweiss Income Plus Arbitrage Active Fund of Funds": "not in TER export",
    "Edelweiss NIFTY PSU Bond Plus SDL Apr 2026 50:50 Index Fund": "matured Apr-2026",
    "Franklin India SHORT TERM INCOME PLAN": "wound up",
    "Franklin India Short-Term Income Plan": "wound up",
    "HSBC Global Equity Climate Change FoF": "no TER disclosure",
    "HSBC Global Equity Climate Change FoF - Dir (G)": "no TER disclosure",
    "ICICI Prudential BSE Insurance ETF": "not in TER export",
    "Mirae Asset Nifty AAA PSU Bond Plus SDL Apr 2026 50:50 Index Fund": "matured Apr-2026",
    "Mirae Asset Nifty AAA PSU Bond Plus SDL Apr 2026 50:50 Index Fund-Dir (G)": "matured Apr-2026",
    "Mirae Asset Nifty AAA PSU Bond Plus SDL Apr 2026 50:50 Index Fund-Reg (G)": "matured Apr-2026",
    "Motilal Oswal Nifty Metal ETF": "not in TER export",
    "Motilal Oswal Nifty Oil & Gas ETF": "not in TER export",
    "NJ Momentum Fund": "no TER disclosure",
    "NJ Value Fund": "no TER disclosure",
    "Nippon India ETF Nifty SDL Apr 2026 Top 20 Equal Weight": "matured Apr-2026",
    "The Wealth Company Mid Cap Fund": "not in TER export",
    "UTI": "invalid universe row",
    "UTI - GILT FUND - Discontinued PF Plan": "discontinued",
    "UTI NIFTY SDL Plus AAA PSU Bond Apr 2026 75:25 Index Fund": "matured Apr-2026",
}

_ALIAS_KEYS = {_key(k): v for k, v in _ALIASES.items()}
_ALIAS_VALUE_KEYS = {_key(v) for v in _ALIASES.values()}
_REASON_BY_KEY = {_key(k): v for k, v in MISSING_REASON.items()}

# Aliases keyed on the universe *display* name, for cases where NAVAll /
# fund_name_from_nav over-trims the fund name (e.g. ``UTI-Dividend Yield
# Fund.-Growth`` -> ``UTI``) so the fund-level key never reaches the right
# TER scheme.
_DISPLAY_ALIASES: dict[str, str] = {
    "UTI-Dividend Yield Fund (G)": "UTI - Dividend Yield Fund",
    "UTI-Dividend Yield Fund - Direct (G)": "UTI - Dividend Yield Fund",
}
_DISPLAY_ALIAS_KEYS = {_key(k): v for k, v in _DISPLAY_ALIASES.items()}


def _match_lookup(key: str, disp_key: str, ter_lookup: dict[str, pd.Series]) -> pd.Series | None:
    """Find a TER scheme for a normalized name.

    Order of precedence: exact key, display-name alias, curated alias,
    containment (either direction, with a length guard to avoid short or
    ambiguous hits).
    """
    if key in ter_lookup:
        return ter_lookup[key]
    if disp_key in _DISPLAY_ALIAS_KEYS:
        alias_key = _key(_DISPLAY_ALIAS_KEYS[disp_key])
        return ter_lookup.get(alias_key)
    if key in _ALIAS_KEYS:
        alias_key = _key(_ALIAS_KEYS[key])
        return ter_lookup.get(alias_key)
    if key in _ALIAS_VALUE_KEYS:
        # The universe name IS a TER scheme name but normalization made them
        # differ only by alias-target text; nothing to do.
        return None
    if len(key) >= 12:
        for other, rec in ter_lookup.items():
            if other and (key in other or other in key):
                shorter, longer = sorted((key, other), key=len)
                if len(shorter) / len(longer) >= 0.6:
                    return rec
    return None


def map_universe_to_ter(
    universe: pd.DataFrame,
    ter: pd.DataFrame,
    navall: dict[str, str],
) -> pd.DataFrame:
    """Attach July-2026 TER to every universe row.

    The universe is at fund+plan level (Regular/Direct).  TER is at fund level
    with separate Regular (R) and Direct (D) columns, so each universe row is
    matched on its fund-level name and then picks the column matching its
    plan type.  The result adds:
      ter_matched  : bool   whether a TER scheme was found for the fund
      ter_value    : the plan-specific TER (float) or NaN
      ter_status   : ok / no_scheme / empty_plan / not_disclosed / matured...
      ter_scheme   : the TER export scheme name that was matched
    """
    ter_lookup = {row["_key"]: row for _, row in ter.iterrows()}

    out = universe.copy()
    codes = out["Amficode"].dropna().astype(int).astype(str)
    out["_code"] = codes.reindex(out.index).astype(object)
    out["_nav_full"] = out["_code"].map(navall)
    # Prefer the NAVAll-derived fund name; it is the authoritative fund-level
    # name and strips plan/option suffixes cleanly for active schemes.  When
    # NAVAll has no entry (matured/wound-up schemes) fall back to the universe
    # display name.  Guard: if NAVAll over-trims (e.g. ``UTI-Dividend Yield
    # Fund.-Growth`` -> ``UTI``) use the display-derived name instead.
    out["_fund"] = [
        _resolve_fund_name(nf, str(fn))
        for nf, fn in zip(out["_nav_full"], out["Fund Name"])
    ]
    out["_key"] = out["_fund"].apply(_key)
    out["_disp_key"] = out["Fund Name"].apply(lambda x: _key(str(x)))

    rows = []
    for _, r in out.iterrows():
        rec = _match_lookup(r["_key"], r["_disp_key"], ter_lookup)
        if rec is None:
            reason = _REASON_BY_KEY.get(r["_key"], "not in TER export")
            rows.append(("no_scheme", float("nan"), None, reason))
            continue
        col = COL_R if str(r["Type"]).strip().lower() == "regular" else COL_D
        val = rec[col]
        if pd.isna(val) or str(val).strip() == "":
            rows.append(("empty_plan", float("nan"), rec[COL_NAME], "plan not disclosed"))
            continue
        rows.append(("ok", float(val), rec[COL_NAME], "matched"))

    out["ter_status"] = [x[0] for x in rows]
    out["ter_value"] = [x[1] for x in rows]
    out["ter_scheme"] = [x[2] for x in rows]
    out["ter_reason"] = [x[3] for x in rows]
    out["ter_matched"] = out["ter_status"] == "ok"
    return out


def build_report(
    universe: pd.DataFrame,
    ter: pd.DataFrame,
    navall: dict[str, str],
) -> dict:
    """Produce the summary statistics for the month's TER coverage."""
    mapped = map_universe_to_ter(universe, ter, navall)
    total_rows = len(mapped)
    matched_rows = int(mapped["ter_matched"].sum())
    missing_rows = total_rows - matched_rows

    funds = (
        mapped.groupby("_fund")["ter_matched"]
        .any()
        .rename("any_matched")
        .reset_index()
    )
    total_funds = len(funds)
    matched_funds = int(funds["any_matched"].sum())
    missing_funds = total_funds - matched_funds

    miss = mapped[~mapped["ter_matched"]]

    return {
        "total_rows": total_rows,
        "matched_rows": matched_rows,
        "missing_rows": missing_rows,
        "total_funds": total_funds,
        "matched_funds": matched_funds,
        "missing_funds": missing_funds,
        "missing_reasons": miss["ter_reason"].value_counts().to_dict(),
        "mapped": mapped,
    }