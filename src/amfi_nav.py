"""AMFI NAVAll.txt scheme dictionary.

Parses the AMFI ``NAVAll.txt`` snapshot once and exposes a per-AMC dictionary of
*fund-level* scheme names (plan/option variants stripped).  This is the
authoritative list of "schemes that must exist" used by the PDF splitter agent to
(a) detect which page starts a scheme's factsheet section and (b) name the
section correctly even when the PDF's own first-line heuristics fail.

The variant-stripping and name-normalisation rules live here so both
``find_missing_schemes.py`` and ``src/pdf_agents.py`` share exactly the same
view of the AMFI scheme universe.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

DATE_FORMAT = "%d-%b-%Y"

# Words after which the remainder of an AMFI scheme name is a plan / option
# variant (see find_missing_schemes.py for the reasoning behind each rule).
R_A1 = re.compile(r"[ -]*-[ -]*(?:DIRECT|REGULAR|INSTITUTIONAL|RETAIL|INCOME|DISTRIBUTION|WITHDRAWAL)\b", re.I)
R_A2 = re.compile(
    r" (?:DIRECT|REGULAR|INSTITUTIONAL|RETAIL)"
    r"(?=\s*(?:PLAN|OPTION|IDCW|BONUS|GROWTH|DIVIDEND|MONTHLY|WEEKLY|DAILY|QUARTERLY|"
    r"HALF[ -]?YEARLY|ANNUAL|FORTNIGHTLY|PAYOUT|REINVESTMENT|INCOME|DISTRIBUTION|WITHDRAWAL|"
    r"CAPITAL)\b|\s*-\s|$)",
    re.I,
)
R_A3 = re.compile(
    r"[()]\s*(?:DIRECT|REGULAR|INSTITUTIONAL|RETAIL)\b"
    r"(?=[)\s]*(?:PLAN|OPTION|IDCW|BONUS|GROWTH|DIVIDEND|MONTHLY|WEEKLY|DAILY|QUARTERLY|"
    r"HALF[ -]?YEARLY|ANNUAL|FORTNIGHTLY|PAYOUT|REINVESTMENT)\b|[)\s]*$)",
    re.I,
)
R_B = re.compile(
    r"(?<![a-z0-9])[ -]*-[ -]*(?:GROWTH|DIVIDEND|IDCW|BONUS|PAYOUT|REINVESTMENT|INCOME|"
    r"DISTRIBUTION|WITHDRAWAL)\b",
    re.I,
)
R_B2 = re.compile(r"(?:^|[\s-])(?:GROWTH|DIVIDEND)\s+(?:OPTION|PLAN)\b", re.I)
R_D = re.compile(r"-\s*\(\s*(?:GROWTH|DIVIDEND|IDCW|PAYOUT|INCOME|DISTRIBUTION)\b", re.I)
R_C = re.compile(
    r"(?<![a-z0-9])[ -]*-[ -]*(?:MONTHLY|WEEKLY|DAILY|QUARTERLY|HALF[ -]?YEARLY|"
    r"ANNUAL|FORTNIGHTLY)(?=\s*(?:IDCW|DIVIDEND|PAYOUT|REINVESTMENT|OPTION)\b|\s*$)",
    re.I,
)
# E: variant directly attached to the fund name without a leading space
# ("Overnight fund- Growth", "Fund- Unclaimed IDCW Investor Education Plan").
R_ATTACH = re.compile(
    r"(?<=[a-z0-9])\s*-\s*(?:DIRECT|REGULAR|GROWTH|DIVIDEND|IDCW|BONUS|PAYOUT|"
    r"REINVESTMENT|INCOME|DISTRIBUTION|WITHDRAWAL|UNCLAIMED(?:\s+[A-Za-z ]+)?)\b",
    re.I,
)
# F: "X- Plan A", "IV- Series A", "- Segregated Portfolio 1" close-out variants
# and trailing "- Dividend Payout Option" (with or without preceding space).
R_PLAN = re.compile(
    r"\s*-\s*(?:[A-Z0-9]+\s*)?(?:PLAN|SERIES|SEGREGATED PORTFOLIO)\b.*$",
    re.I,
)
R_PAYOUT = re.compile(
    r"\s*-\s*(?:DIVIDEND\s+PAYOUT|MONTHLY|WEEKLY|QUARTERLY|HALF[- ]YEARLY|"
    r"ANNUAL|DIRECT|REGULAR)\s*(?:OPTION|PLAN)?\b.*$",
    re.I,
)
_VARIANT_RES = (R_A1, R_A2, R_A3, R_B, R_B2, R_C, R_D, R_ATTACH, R_PLAN, R_PAYOUT)


def fund_name_from_nav(scheme_name: str) -> str:
    """Strip plan / option variants from an AMFI scheme name -> fund name."""
    name = scheme_name.strip()
    cuts = [m.start() for rx in _VARIANT_RES for m in [rx.search(name)] if m]
    if cuts:
        name = name[: min(cuts)].rstrip(" -")
    name = re.sub(r"[\s.\-]+$", "", name).strip()
    while name and name[-1] in "()" and name.count("(") != name.count(")"):
        name = name[:-1]
    name = re.sub(r"\s+", " ", name).strip()
    return name or scheme_name.strip()


def norm(text: str) -> str:
    """Normalize a fund name for comparison."""
    s = (text or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\bthe\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def amc_brand_words(amc: str) -> set[str]:
    """AMC brand words, e.g. 'Mirae Asset Mutual Fund' -> {'mirae', 'asset', 'mf'}."""
    b = re.sub(r"\bMutual Fund\b", "mf", amc, flags=re.I)
    b = re.sub(r"\([^)]*\)", "", b)
    return set(norm(b).split())


def _parse_rows(text: str) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str | None, str, str, str]] = []
    current_amc: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("Scheme Code"):
            continue
        parts = s.split(";")
        if len(parts) == 6:
            code, _, _, name, _nav, date = parts
            rows.append((current_amc, code, name, date))
        elif "Mutual Fund" in s:
            current_amc = s
    return [(a, c, n, d) for a, c, n, d in rows if a is not None]


class AmfiNav:
    """Parsed NAVAll.txt with a per-AMC fund-level scheme dictionary."""

    def __init__(self, nav_path: Path):
        self.path = Path(nav_path)
        self.rows: list[tuple[str, str, str, str]] = []
        self.target_date: datetime | None = None
        self.funds: dict[str, list[str]] = {}       # amc_lower -> fund names
        self.norm_funds: dict[str, set[str]] = {}   # amc_lower -> normalized set
        self.brands: dict[str, set[str]] = {}       # amc_lower -> brand words
        self._load()

    def _load(self) -> None:
        with open(self.path, encoding="utf-8") as f:
            rows = _parse_rows(f.read())
        dated = [
            (a, c, n, datetime.strptime(d, DATE_FORMAT))
            for a, c, n, d in rows
            if a is not None
        ]
        if not dated:
            return
        # The most recent *weekday* NAV date (used for reporting).
        business = [t for t in dated if t[3].weekday() < 5]
        if business:
            self.target_date = max(t[3] for t in business)
        # Fund universe = each scheme code's OWN most-recent NAV row.  Overnight
        # / liquid funds also publish NAV on weekends, so a single weekday-only
        # snapshot date would silently drop them from the dictionary and they
        # would never be matched (or flagged missing) by the coverage checks.
        best: dict[tuple[str, str], tuple[str, str, str, datetime]] = {}
        for a, c, n, dt in dated:
            key = (a.strip().lower(), c)
            if key not in best or dt > best[key][3]:
                best[key] = (a, c, n, dt)
        self.rows = [
            (amc, code, name, dt.strftime(DATE_FORMAT))
            for (amc, code, name, dt) in best.values()
        ]
        for amc, code, name, date in self.rows:
            key = amc.strip().lower()
            fund = fund_name_from_nav(name)
            if fund not in self.funds.setdefault(key, []):
                self.funds[key].append(fund)
            self.norm_funds.setdefault(key, set()).add(norm(fund))
            self.brands.setdefault(key, amc_brand_words(amc))

    def fund_names(self, amc_name: str) -> list[str]:
        # De-duplicate by normalized name (NAVAll sometimes lists the same fund
        # under differently-cased names, which would break single-match logic).
        seen: dict[str, str] = {}
        for f in self.funds.get(amc_name.strip().lower(), []):
            seen.setdefault(norm(f), f)
        return list(seen.values())

    def normalized(self, amc_name: str) -> set[str]:
        return self.norm_funds.get(amc_name.strip().lower(), set())

    def brand_words(self, amc_name: str) -> set[str]:
        return self.brands.get(amc_name.strip().lower(), set())

    def amc_keys(self) -> list[str]:
        return sorted(self.funds.keys())

    def total_funds(self) -> int:
        return sum(len(v) for v in self.funds.values())

    @staticmethod
    def default_path() -> Path:
        from src.pdf_agents import PROJECT_ROOT

        return PROJECT_ROOT / "data" / "universe" / "navall.txt"


_DEFAULT: AmfiNav | None = None


def get_nav() -> AmfiNav | None:
    """Return a lazily-loaded, cached AmfiNav (None if navall.txt is absent)."""
    global _DEFAULT
    if _DEFAULT is None:
        p = AmfiNav.default_path()
        if not p.exists():
            return None
        _DEFAULT = AmfiNav(p)
    return _DEFAULT
