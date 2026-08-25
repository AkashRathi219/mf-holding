"""Canonical financial-statement schema [stmt-v1.0.0].

One home for:
- CANONICAL_ITEMS: every line item the statement pipeline can emit, grouped
  by statement (income / balance_sheet / cash_flow), with sign conventions.
- LABEL_ALIASES: Indian-GAAP (Schedule III) label vocabulary -> canonical
  key. Matching is normalised (lowercase, punctuation stripped) with a
  token-overlap fallback; unmatched labels are kept raw and flagged.
- UNIT detection: filings print "Rs. in Lakhs / Crore / Million" — values
  are NORMALISED TO CRORE at extraction time so downstream ratios never
  re-scale again.
- PERIOD parsing: Reg-33 columns are Quarter/Half-year/Nine-months/FY ended
  dates; cumulative periods derive discrete quarters by chain subtraction.

Values are stored in Rs. crore as floats; per-share values (EPS, DPS,
face value) stay in rupees and are marked in PER_SHARE_KEYS.
"""
from __future__ import annotations

import math
import re

SCHEMA_VERSION = "stmt-v1.0.0"

# ---- canonical line items ------------------------------------------------------

INCOME_ITEMS = (
    "revenue_from_operations", "other_income", "total_income",
    "cost_of_materials", "purchases_stock_in_trade", "changes_in_inventories",
    "employee_benefits", "finance_costs", "depreciation_amortisation",
    "other_expenses", "total_expenses", "ebitda",        # ebitda derived
    "exceptional_items", "pbt", "tax_expense", "pat",
    "share_of_associates", "pat_after_associates",
    "oci_total", "total_comprehensive_income",
    "minority_interest", "eps_basic", "eps_diluted",
)

BALANCE_SHEET_ITEMS = (
    "share_capital", "reserves_surplus", "total_equity",
    "borrowings_non_current", "borrowings_current", "total_debt",  # derived
    "deferred_tax_liability", "deferred_tax_asset",
    "ppe_net", "cwip", "goodwill_intangibles", "investments_non_current",
    "investments_current", "loans_advances_non_current",
    "inventory", "trade_receivables", "cash_equivalents",
    "loans_advances_current", "other_current_assets",
    "total_non_current_assets", "total_current_assets", "total_assets",
    "trade_payables", "other_current_liabilities",
    "provisions_current", "provisions_non_current",
    "total_current_liabilities", "total_liabilities",   # often derived
    "net_worth",                                        # alias of total_equity
)

CASHFLOW_ITEMS = (
    "cfo", "cfi", "cff", "capex", "dividends_paid", "tax_paid",
    "net_change_in_cash", "opening_cash", "closing_cash",
)

CANONICAL_ITEMS = {
    "income": INCOME_ITEMS,
    "balance_sheet": BALANCE_SHEET_ITEMS,
    "cash_flow": CASHFLOW_ITEMS,
}

PER_SHARE_KEYS = frozenset((
    "eps_basic", "eps_diluted", "dps", "face_value", "book_value_per_share",
))

# canonical keys that may legitimately be negative and are NOT errors
SIGNED_OK = frozenset((
    "changes_in_inventories", "exceptional_items", "pat", "pbt",
    "tax_expense", "oci_total", "total_comprehensive_income",
    "share_of_associates", "minority_interest", "net_change_in_cash",
    "cfi", "cff", "cfo", "capex", "dividends_paid", "tax_paid",
))

DERIVED_KEYS = frozenset(("ebitda", "total_debt", "total_liabilities"))

# ---- label aliases -------------------------------------------------------------

_LABEL_ALIASES: dict[str, str] = {}


def _alias(canon: str, *labels: str) -> None:
    for lab in labels:
        _LABEL_ALIASES[_norm(lab)] = canon


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)          # drop bracketed qualifiers
    s = re.sub(r"[^a-z0-9&/ ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_alias("revenue_from_operations", "revenue from operations", "net sales",
       "sales", "income from operations", "total income from operations",
       "operating revenue")
_alias("other_income", "other income", "other operating income")
_alias("total_income", "total income", "total revenue")
_alias("cost_of_materials", "cost of materials consumed",
       "raw materials consumed", "cost of materials",
       "consumption of raw materials")
_alias("purchases_stock_in_trade", "purchases of stock-in-trade",
       "purchases of traded goods", "purchases", "stock in trade purchases")
_alias("changes_in_inventories", "increases/decreases in inventories",
       "changes in inventories",
       "(increase)/decrease in inventories",
       "changes in inventories of finished goods")
_alias("employee_benefits", "employee benefits expense", "employee costs",
       "personnel expenses", "employees expense", "staff cost")
_alias("finance_costs", "finance costs", "finance cost", "interest expense",
       "interest and finance charges", "borrowing costs", "interest cost")
_alias("depreciation_amortisation",
       "depreciation and amortisation expense",
       "depreciation & amortization", "depreciation",
       "depreciation, depletion and amortisation")
_alias("other_expenses", "other expenses", "other operating expenses",
       "administrative and general expenses", "operating expenses")
_alias("total_expenses", "total expenses", "total expenditure")
_alias("exceptional_items", "exceptional items", "extraordinary items")
_alias("pbt", "profit before tax", "profit/(loss) before tax",
       "profit before exceptional items and tax",
       "profit before tax and exceptional items",
       "profit before share of associates and tax")
_alias("tax_expense", "tax expense", "current tax", "total tax expense",
       "provision for taxation", "taxation", "income tax expense")
_alias("pat", "profit after tax", "profit/(loss) after tax", "net profit",
       "profit for the period", "profit for the year",
       "net profit/(loss) after tax", "profit after taxation")
_alias("share_of_associates",
       "share of profit/loss of associates and joint ventures",
       "share of profit of associates", "share of profit in associates")
_alias("pat_after_associates",
       "profit after tax and share of profit/loss of associates",
       "profit after share of profit of associates",
       "profit after associates and joint ventures")
_alias("total_comprehensive_income", "total comprehensive income",
       "total comprehensive income for the period")
_alias("eps_basic", "basic eps", "earnings per share basic",
       "basic earnings per share", "eps basic", "basic eps (rs)",
       "earnings per equity share basic")
_alias("eps_diluted", "diluted eps", "earnings per share diluted",
       "diluted earnings per share", "earnings per equity share diluted")

_alias("share_capital", "share capital", "equity share capital",
       "paid-up share capital", "paid up equity share capital")
_alias("reserves_surplus", "reserves and surplus", "other equity",
       "reserves & surplus", "capital and reserves", "reserves")
_alias("total_equity", "total equity", "total shareholders funds",
       "shareholders funds", "total shareholders' funds", "equity funds",
       "total owners equity", "owners funds")
_alias("borrowings_non_current", "long term borrowings",
       "non-current borrowings", "long-term borrowings",
       " borrowings non current", "non current borrowings")
_alias("borrowings_current", "short term borrowings",
       "current borrowings", "short-term borrowings",
       "loans repayable on demand")
_alias("total_debt", "total borrowings", "total debt",
       "borrowings")
_alias("deferred_tax_liability", "deferred tax liabilities net",
       "deferred tax liability", "deferred tax liabilities")
_alias("deferred_tax_asset", "deferred tax assets net",
       "deferred tax asset", "deferred tax assets")
_alias("ppe_net", "property plant and equipment",
       "property, plant & equipment", "net block", "fixed assets",
       "tangible assets net block", "right of use assets",
       "intangible assets", "tangible assets")
_alias("cwip", "capital work-in-progress", "capital work in progress",
       "cwip")
_alias("goodwill_intangibles", "goodwill", "intangible assets under dev",
       "goodwill and intangible assets", "other intangible assets")
_alias("investments_non_current", "non-current investments",
       "non current investments", "long term investments", "investments")
_alias("investments_current", "current investments",
       "short term investments")
_alias("inventory", "inventories", "inventory", "stock in trade",
       "stocks", "stores and spares")
_alias("trade_receivables", "trade receivables", "sundry debtors",
       "accounts receivable", "trade receivables net", "debtors")
_alias("cash_equivalents", "cash and cash equivalents",
       "cash and bank balances", "cash and bank balance",
       "balances with banks", "cash")
_alias("total_current_assets", "total current assets")
_alias("total_non_current_assets", "total non-current assets",
       "total non current assets")
_alias("total_assets", "total assets", "total assets & liabilities",
       "total capital and liabilities", "balance sheet total",
       "total funds employed", "capital and liabilities")
_alias("trade_payables", "trade payables", "sundry creditors",
       "accounts payable", "creditors", "trade payables micro small",
       "trade payables others")
_alias("other_current_liabilities", "other current liabilities",
       "other current financial liabilities", "other financial liabilities")
_alias("provisions_current", "provisions current", "current provisions")
_alias("provisions_non_current", "provisions non current")
_alias("total_current_liabilities", "total current liabilities")
_alias("cfo", "net cash generated from operating activities",
       "net cash flow from operating activities",
       "cash generated from operations", "operating activities",
       "net cash from operating activities",
       "net cash generated by operating activities")
_alias("cfi", "net cash used in investing activities",
       "net cash flow from investing activities", "investing activities",
       "net cash from investing activities")
_alias("cff", "net cash used in financing activities",
       "net cash flow from financing activities", "financing activities",
       "net cash from financing activities")
_alias("capex", "purchase of property plant and equipment",
       "purchase of fixed assets", "capital expenditure",
       "acquisition of fixed assets", "purchase of ppe",
       "payment for acquisition of property plant and equipment")
_alias("dividends_paid", "dividends paid", "dividend paid including tax",
       "equity dividend paid")
_alias("net_change_in_cash", "net increase/(decrease) in cash",
       "net increase decrease in cash", "net change in cash")
_alias("closing_cash", "closing cash and cash equivalents",
       "cash and cash equivalents at end of the period",
       "closing balance")
_alias("opening_cash", "opening cash and cash equivalents",
       "cash and cash equivalents at beginning of period")


def is_exact_label(label: str) -> bool:
    """True when the label hits an alias verbatim (not via fuzzy scoring)."""
    return _norm(label) in _LABEL_ALIASES


def match_label(label: str, min_score: float = 0.72) -> tuple[str | None, float]:
    """Fuzzy-map a filing label to a canonical key.

    Exact normalised hits return immediately; otherwise best token-Jaccard
    wins if it clears ``min_score``. Returns (canonical_key|None, score).
    """
    key = _norm(label)
    if not key:
        return None, 0.0
    hit = _LABEL_ALIASES.get(key)
    if hit:
        return hit, 1.0
    ktoks = set(key.split())
    if not ktoks:
        return None, 0.0
    best_key, best_score = None, 0.0
    for cand, canon in _LABEL_ALIASES.items():
        ctoks = set(cand.split())
        inter = len(ktoks & ctoks)
        if not inter:
            continue
        union = len(ktoks | ctoks)
        score = inter / union
        # strong bonus when the candidate starts the label (common prefix)
        if key.startswith(cand[:8]) or cand.startswith(key[:8]):
            score += 0.15
        if score > best_score:
            best_key, best_score = canon, score
    if best_score >= min_score:
        return best_key, min(1.0, best_score)
    return None, best_score


# ---- unit scaling --------------------------------------------------------------

_UNIT_PATTERNS = (
    (re.compile(r"(₹|rs\.?|inr)\s*(amount)?\s*in\s*(lakhs?|lacs?|lac)\b", re.I),
     0.10),                                   # lakh -> crore
    (re.compile(r"(₹|rs\.?|inr)\s*in\s*crores?\b", re.I), 1.0),
    (re.compile(r"in\s+crore", re.I), 1.0),
    (re.compile(r"(₹|rs\.?|inr)?\s*in\s*millions?\b", re.I), 10.0),
    (re.compile(r"figures?\s+in\s+(lakhs?|lacs?)\b", re.I), 0.10),
)


def unit_scale_to_crore(page_text: str) -> float:
    """Scale factor that converts printed figures to Rs crore (1.0 unknown)."""
    for pat, scale in _UNIT_PATTERNS:
        if pat.search(page_text or ""):
            return scale
    return 1.0


def to_number(token: str) -> float | None:
    """Parse a printed numeric cell: '325,290', '(4,214)', '12.35', '-'."""
    t = (token or "").strip().replace("\u20b9", "").replace(",", "")
    if t in ("", "-", "--", "\u2014", "na", "NA"):
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    if t.endswith("-") and t[:-1].replace(".", "").isdigit():
        neg, t = True, t[:-1]
    try:
        v = float(t)
    except ValueError:
        return None
    if math.isnan(v):
        return None
    return -v if neg else v


# ---- period parsing --------------------------------------------------------------

_MONTH_NUM = {m.lower(): i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
for _abbr, _num in (("jan", 1), ("feb", 2), ("mar", 3), ("apr", 4),
                    ("may", 5), ("jun", 6), ("jul", 7), ("aug", 8),
                    ("sep", 9), ("sept", 9), ("oct", 10), ("nov", 11),
                    ("dec", 12)):
    _MONTH_NUM.setdefault(_abbr, _num)

_DATE_PATTERNS = (
    # June 30, 2026 (month-first headline form — most specific, try first)
    re.compile(r"(january|february|march|april|may|june|july|august|"
               r"september|october|november|december)\s+(\d{1,2})"
               r"(?:st|nd|rd|th)?[,\s]+(\d{4})", re.I),
    # 30th June 2026 / 30 June 2026 (day-first with explicit day)
    re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
               r"(january|february|march|april|may|june|july|august|"
               r"september|october|november|december)"
               r"[,\s]*(\d{4})", re.I),
    # 30-06-2026 / 30/06/2026
    re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$"),
    # Jun'26 / Jun-26 / Jun.26 (header shorthand, no day)
    re.compile(r"(january|february|march|april|may|june|july|august|"
               r"september|october|november|december|jan|feb|mar|apr|jun|"
               r"jul|aug|sept|sep|oct|nov|dec)['\-\. ]*(\d{2,4})\b(?!\s*,)",
               re.I),
)

_PERIOD_KINDS = (
    (re.compile(r"nine\s*months?", re.I), "9M"),
    (re.compile(r"half\s*year|h1\b|first half", re.I), "H1"),
    (re.compile(r"quarter", re.I), "Q"),
    (re.compile(r"year\s*ended|full\s*year|\bFY\b", re.I), "FY"),
)


def parse_period_date(text: str) -> tuple[int, int, int] | None:
    """Extract (year, month, day) from a column-header fragment or headline."""
    t = (text or "").strip()
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if iso:                                   # AI tier emits ISO directly
        y_n, m_n, d_n = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        if 2000 <= y_n <= 2100 and 1 <= m_n <= 12 and 1 <= d_n <= 31:
            return y_n, m_n, d_n
        return None
    for pi, pat in enumerate(_DATE_PATTERNS):
        m = pat.search(t)
        if not m:
            continue
        groups = m.groups()
        if pi == 0:                          # Month DD, YYYY
            mon_g, day_g, year_g = groups
            month_n = _MONTH_NUM.get(mon_g.lower(), 0)
            day_n, year = int(day_g), year_g
        elif pi == 1:                        # DD Month YYYY
            day_g, mon_g, year_g = groups
            month_n = _MONTH_NUM.get(mon_g.lower(), 0)
            day_n, year = int(day_g), year_g
        elif pi == 2:                        # DD-MM-YYYY
            month_n, day_n = int(groups[1]), int(groups[0])
            year = groups[2]
        else:                                # Mon'YY shorthand -> day=1
            mon_g, year = groups
            day_n = 1
            month_n = _MONTH_NUM.get(mon_g.lower().rstrip("."), 0)
        try:
            year_n = int(year)
        except (TypeError, ValueError):
            continue
        if year_n < 100:
            year_n += 2000
        if not (2000 <= year_n <= 2100 and 1 <= month_n <= 12
                and 1 <= day_n <= 31):
            continue
        return year_n, month_n, day_n
    return None


def primary_period_from_headline(headline: str) -> tuple[str, tuple] | None:
    """('Q'|'H1'|'9M'|'FY', (y,m,d)) declared by the filing headline."""
    t = headline or ""
    kind = classify_period(t, default="Q")
    d = parse_period_date(t)
    if not d:
        return None
    return kind, d


def classify_period(text: str, default: str = "Q") -> str:
    """Quarter / H1 / 9M / FY classification from surrounding header words."""
    for pat, kind in _PERIOD_KINDS:
        if kind == "Q" and re.search(r"quarter", text or "", re.I):
            return "Q"
        if kind != "Q" and pat.search(text or ""):
            return kind
    return default


def fiscal_year(period_end: tuple[int, int], kind: str) -> str:
    """Indian FY label: Apr-Mar boundary (e.g. (2026,6) -> FY27)."""
    y, m = period_end
    fy_start = y - 1 if m <= 3 else y
    return f"FY{str(fy_start + 1)[-2:]}"


_QUARTER_BY_MONTH = {4: "Q1", 5: "Q1", 6: "Q1", 7: "Q2", 8: "Q2", 9: "Q2",
                     10: "Q3", 11: "Q3", 12: "Q3", 1: "Q4", 2: "Q4",
                     3: "Q4"}


def quarter_of_month(month: int, kind: str = "Q") -> str | None:
    """Discrete-quarter tag from an end month (Apr-Jun=Q1 ... Jan-Mar=Q4).
    Applies whenever the column is a true quarter (kind == 'Q'); cumulative
    kinds (H1/9M/FY) have no single quarter."""
    if kind != "Q":
        return None
    return _QUARTER_BY_MONTH.get(month)


def validate_statement(rec: dict) -> list[str]:
    """Identity checks on one normalised period record. Returns issue list;
    empty means clean. Values are Rs crore floats (per-share items exempt)."""
    issues: list[str] = []

    def g(k):
        v = rec.get(k)
        return float(v) if isinstance(v, (int, float)) else None

    ta, te, tl = g("total_assets"), g("total_equity"), g("total_liabilities")
    # A = E + L only checkable when liabilities were explicitly parsed
    if ta and te is not None and tl is not None and abs(ta) > 1e-9:
        if abs(ta - (te + tl)) / abs(ta) > 0.005:
            issues.append(f"identity_assets_ne: A={ta:.1f} vs "
                          f"E+L={te + tl:.1f}")
    rev = g("revenue_from_operations")
    if rev is not None and rev < 0:
        issues.append("revenue_negative")
    pat, pbt_ = g("pat"), g("pbt")
    if pat is not None and pbt_ is not None and pbt_ != 0:
        if abs(pat) > abs(pbt_) * 3 + 1.0:
            issues.append(f"pat_exceeds_pbt: PAT={pat:.1f} PBT={pbt_:.1f}")
    exp, ti = g("total_expenses"), g("total_income") or rev
    if exp is not None and ti is not None and ti > 0 and exp > ti * 3 + 100:
        issues.append("expenses_far_above_income")
    return issues


def derive_ebitda(rec: dict) -> dict:
    """Fill derived keys honestly: ebitda = total_income − total_expenses
    (+ exceptional add-back) when inputs exist; total_debt = LT + ST."""
    out = dict(rec)

    def g(k):
        v = out.get(k)
        return float(v) if isinstance(v, (int, float)) else None

    if g("ebitda") is None:
        ti = g("total_income") or g("revenue_from_operations")
        ex = g("total_expenses")
        dep = g("depreciation_amortisation")
        fin = g("finance_costs")
        exc = g("exceptional_items") or 0.0
        if ti is not None and ex is not None and dep is not None \
                and fin is not None:
            out["ebitda"] = ti - ex + dep + fin - exc
    if g("total_debt") is None:
        lt, st = g("borrowings_non_current"), g("borrowings_current")
        if lt is not None or st is not None:
            out["total_debt"] = (lt or 0.0) + (st or 0.0)
    if g("net_worth") is None and g("total_equity") is not None:
        out["net_worth"] = g("total_equity")
    if g("total_liabilities") is None:
        ta, eq = g("total_assets"), g("total_equity")
        if ta is not None and eq is not None:
            out["total_liabilities"] = ta - eq
    return out
