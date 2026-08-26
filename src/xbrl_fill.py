"""XBRL ingestion pipeline [stmt-v1.0.0].

Converts the bulk-downloaded NSE financial-results XBRL filings
(``data/raw/financial_results_xbrl/<SYMBOL>/<SYMBOL>_<seq>.xml`` +
``_metadata.json``, produced by ``src/financial_statements.py
--download-fr-xbrl``) into the SAME canonical document shape as the PDF
pipeline (``data/stock_financials/<ISIN>.json``), replacing its 3-symbol
pilot coverage with ~710 covered symbols.

Output document mirrors :func:`financial_statements.process_stock` exactly::

    {schema_version, isin, symbol, name, fetched_at, sources,
     consolidated?: {quarters, annual, ttm}, standalone?: {...},
     validation: {issues, confidence, checked_at},
     _latest_balance_sheet}

plus two consumer-sugar keys the ratio engine reads: the doc-level
``_latest_balance_sheet`` (webapp/stock_fundamental.compute_fundamentals)
and a block-level ``latest_balance_sheet`` (webapp/db.fundamental_snapshot).

Empirical corpus structure (all 17,277 files inspected via tag census):
- Every file is PLAIN XBRL (``xbrli:xbrl`` root, BSE ``in-bse-fin``
  taxonomy); there is NO iXBRL / ``ix:nonFraction`` anywhere.
- Facts carry ABSOLUTE INR amounts under ``unitRef="INR"`` regardless of
  the ``LevelOfRoundingUsedInFinancialStatements`` header; ``decimals="-7"``
  is a precision hint, NOT a scale. Per-share facts use ``INRPerShare``;
  ratios use ``pure`` and are ignored.
- Context ids vary by era (OneD/FourD/PY_I/...); periods are read from the
  ``<xbrli:context>`` definitions themselves (instant vs startDate/endDate),
  never from id naming. Facts inside dimensioned scenarios (segment axes,
  other-expense splits) are excluded to avoid double counting.
- Quarterly filings tag only the P&L (+ segment notes); balance-sheet
  instants appear at half-year/year ends; cash-flow statements ride the
  cumulative H1/9M/FY durations. Bank/NBFC templates swap the top P&L lines
  (InterestEarned / InterestExpended / OperatingExpenses ...) — mapped
  separately below.

Selection policy per (symbol, section, period): Audited beats Un-Audited,
then Consolidated beats Non-Consolidated, then latest filingDate wins.

CLI::

    python -m src.xbrl_fill [--symbols X,Y] [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date as _date, timedelta
from pathlib import Path

from . import statement_schema as ss
from .financial_statements import (
    FINANCIALS_DIR,
    XBRL_META_PATH,
    XBRL_RAW_DIR,
    assemble,
    build_section_records,
    compute_ttm,
    sha256_file,
    validate_and_score,
)
from .stock_common import date_key, now_iso, save_json
from .stock_identity import load_identity

log = logging.getLogger("xbrl_fill")

XBRLI_NS = "http://www.xbrl.org/2003/instance"
SKIP_UNITS = frozenset(("pure", "shares"))
INR_TO_CRORE = 1.0e7

# element local-name -> (canonical key, priority). Higher priority wins when
# several tagged elements map to the same canonical key within one period;
# priorities are spaced by 10 so new aliases slot in without renumbering.
_ELEMENT_MAP: dict[str, tuple[str, int]] = {
    # ---- statement of profit and loss (duration facts) ----
    "RevenueFromOperations": ("revenue_from_operations", 40),
    "InterestEarned": ("revenue_from_operations", 30),
    "OtherIncome": ("other_income", 40),
    "FeesAndCommissionIncome": ("other_income", 10),
    "DividendIncome": ("other_income", 10),
    "RentalIncome": ("other_income", 10),
    "RevenueOnInvestments": ("other_income", 10),
    "NetGainOnFairValueChanges": ("other_income", 10),
    "Income": ("total_income", 40),
    "CostOfMaterialsConsumed": ("cost_of_materials", 40),
    "PurchasesOfStockInTrade": ("purchases_stock_in_trade", 40),
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade":
        ("changes_in_inventories", 40),
    "EmployeeBenefitExpense": ("employee_benefits", 40),
    "PaymentsToAndOnBehalfOfEmployees": ("employee_benefits", 20),
    "EmployeesCost": ("employee_benefits", 30),
    "FinanceCosts": ("finance_costs", 40),
    "InterestExpended": ("finance_costs", 30),
    "DepreciationDepletionAndAmortisationExpense":
        ("depreciation_amortisation", 40),
    "DepreciationAndAmortisationExpense": ("depreciation_amortisation", 20),
    "OtherExpenses": ("other_expenses", 40),
    "OperatingExpenses": ("other_expenses", 30),
    "OtherOperatingExpenses": ("other_expenses", 10),
    "FeesAndCommissionExpense": ("other_expenses", 10),
    "ImpairmentOnFinancialInstruments": ("other_expenses", 10),
    "Expenses": ("total_expenses", 40),
    "ExpenditureExcludingProvisionsAndContingencies": ("total_expenses", 30),
    "ExceptionalItemsBeforeTax": ("exceptional_items", 40),
    "ExceptionalItems": ("exceptional_items", 30),
    "ExtraordinaryItems": ("exceptional_items", 10),
    "ProvisionsOtherThanTaxAndContingencies": ("exceptional_items", 20),
    "ProfitBeforeTax": ("pbt", 40),
    "ProfitLossFromOrdinaryActivitiesBeforeTax": ("pbt", 40),
    "ProfitBeforeExceptionalItemsAndTax": ("pbt", 30),
    "TaxExpense": ("tax_expense", 40),
    "ProfitOrLossAttributableToOwnersOfParent": ("pat", 50),
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates":
        ("pat", 50),
    "ProfitLossForPeriodFromContinuingOperations": ("pat", 30),
    "ProfitLossForThePeriodFromContinuingOperations": ("pat", 30),
    "ProfitLossForPeriod": ("pat", 20),
    "ProfitLossForThePeriod": ("pat", 20),
    "ProfitLossFromOrdinaryActivitiesAfterTax": ("pat", 20),
    "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod":
        ("share_of_associates", 40),
    "ShareOfProfitLossOfAssociates": ("share_of_associates", 30),
    "PatAfterMinorityInterestAndShareOfProfitLossOfAssociates":
        ("pat_after_associates", 30),
    "ProfitOrLossAttributableToNonControllingInterests": ("minority_interest", 30),
    "ProfitLossOfMinorityInterest": ("minority_interest", 30),
    "OtherComprehensiveIncomeNetOfTaxes": ("oci_total", 40),
    "OtherComprehensiveIncome": ("oci_total", 20),
    "ComprehensiveIncomeForThePeriod": ("total_comprehensive_income", 40),
    # per-share (unitRef INRPerShare — never scaled)
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations":
        ("eps_basic", 50),
    "BasicEarningsPerShareAfterExtraordinaryItems": ("eps_basic", 50),
    "BasicEarningsLossPerShareFromContinuingOperations": ("eps_basic", 30),
    "BasicEarningsPerShareBeforeExtraordinaryItems": ("eps_basic", 30),
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations":
        ("eps_diluted", 50),
    "DilutedEarningsPerShareAfterExtraordinaryItems": ("eps_diluted", 50),
    "DilutedEarningsLossPerShareFromContinuingOperations": ("eps_diluted", 30),
    "DilutedEarningsPerShareBeforeExtraordinaryItems": ("eps_diluted", 30),
    "PaidUpValueOfEquityShareCapital": ("share_capital", 30),
    # ---- balance sheet (instant facts) ----
    "EquityShareCapital": ("share_capital", 40),
    "Capital": ("share_capital", 20),
    "OtherEquity": ("reserves_surplus", 40),
    "ReservesAndSurplus": ("reserves_surplus", 40),
    "Equity": ("total_equity", 40),
    "EquityAttributableToOwnersOfParent": ("total_equity", 30),
    "BorrowingsNoncurrent": ("borrowings_non_current", 40),
    "LongTermBorrowings": ("borrowings_non_current", 30),
    "BorrowingsCurrent": ("borrowings_current", 40),
    "ShortTermBorrowings": ("borrowings_current", 30),
    "DeferredTaxLiabilitiesNet": ("deferred_tax_liability", 40),
    "DeferredTaxAssetsNet": ("deferred_tax_asset", 40),
    "PropertyPlantAndEquipment": ("ppe_net", 40),
    "FixedAssets": ("ppe_net", 20),
    "InvestmentProperty": ("ppe_net", 10),
    "CapitalWorkInProgress": ("cwip", 40),
    "NoncurrentInvestments": ("investments_non_current", 40),
    "InvestmentsAccountedForUsingEquityMethod": ("investments_non_current", 30),
    "Investments": ("investments_non_current", 10),
    "CurrentInvestments": ("investments_current", 40),
    "LoansNoncurrent": ("loans_advances_non_current", 40),
    "LoansCurrent": ("loans_advances_current", 40),
    "Advances": ("loans_advances_current", 10),
    "Inventories": ("inventory", 40),
    "TradeReceivablesCurrent": ("trade_receivables", 40),
    "TradeReceivablesNoncurrent": ("trade_receivables", 40),
    "TradeReceivables": ("trade_receivables", 30),
    "CashAndCashEquivalents": ("cash_equivalents", 40),
    "BankBalanceOtherThanCashAndCashEquivalents": ("cash_equivalents", 40),
    "CashAndBalancesWithReserveBankOfIndia": ("cash_equivalents", 30),
    "BalancesWithBanksAndMoneyAtCallAndShortNotice": ("cash_equivalents", 30),
    "OtherCurrentAssets": ("other_current_assets", 40),
    "CurrentTaxAssets": ("other_current_assets", 30),
    "OtherCurrentLiabilities": ("other_current_liabilities", 40),
    "CurrentTaxLiabilities": ("other_current_liabilities", 30),
    "TradePayablesCurrent": ("trade_payables", 40),
    "TradePayablesNoncurrent": ("trade_payables", 40),
    "ProvisionsCurrent": ("provisions_current", 40),
    "ProvisionsNoncurrent": ("provisions_non_current", 40),
    "NoncurrentAssets": ("total_non_current_assets", 40),
    "CurrentAssets": ("total_current_assets", 40),
    "Assets": ("total_assets", 40),
    "EquityAndLiabilities": ("total_assets", 30),
    "CapitalAndLiabilities": ("total_assets", 20),
    "Liabilities": ("total_liabilities", 40),
    "CurrentLiabilities": ("total_current_liabilities", 40),
    "CurrentLiabilitiesAndProvisions": ("total_current_liabilities", 30),
    # ---- cash-flow statement (cumulative duration facts) ----
    "CashFlowsFromUsedInOperatingActivities": ("cfo", 40),
    "CashFlowsFromUsedInInvestingActivities": ("cfi", 40),
    "CashFlowsFromUsedInFinancingActivities": ("cff", 40),
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities":
        ("capex", 40),
    "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities": ("capex", 30),
    "PurchaseOfInvestmentPropertyClassifiedAsInvestingActivities":
        ("capex", 30),
    "PurchaseOfGoodwillClassifiedAsInvestingActivities": ("capex", 20),
    "PurchaseOfBiologicalAssetsOtherThanBearerPlantsClassifiedAsInvestingActivities":
        ("capex", 20),
    "PurchaseOfOtherLongTermAssetsClassifiedAsInvestingActivities":
        ("capex", 20),
    "PurchaseOfIntangibleAssetsUnderDevelopment": ("capex", 20),
    "DividendsPaidClassifiedAsFinancingActivities": ("dividends_paid", 40),
    "IncreaseDecreaseInCashAndCashEquivalentsBeforeEffectOfExchangeRateChanges":
        ("net_change_in_cash", 40),
    "IncreaseDecreaseInCashAndCashEquivalents": ("net_change_in_cash", 30),
    "CashAndCashEquivalentsCashFlowStatement": ("closing_cash", 40),
}

# Schedule III merges several tagged lines into one canonical bucket; the
# stored value is the SUM of every group member present in the period (the
# members are disjoint filing lines; totals like CurrentFinancialAssets are
# deliberately NOT members so buckets never double count).
_SUM_GROUPS: frozenset[str] = frozenset((
    "goodwill_intangibles", "trade_receivables", "cash_equivalents",
    "investments_non_current", "trade_payables", "capex",
    "other_current_assets", "other_current_liabilities", "ppe_net",
))

# header/document facts worth keeping off-record
_INFO_ELEMENTS = frozenset((
    "Symbol", "NameOfTheCompany", "NameOfBank", "ISIN",
    "WhetherResultsAreAuditedOrUnaudited",
    "NatureOfReportStandaloneConsolidated",
    "DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod",
    "DateOfStartOfFinancialYear", "DateOfEndOfFinancialYear",
    "LevelOfRoundingUsedInFinancialStatements", "ReportingQuarter",
))

_PER_SHARE_CANONS = ss.PER_SHARE_KEYS | {"face_value"}

_BS_KEYS = ("total_equity", "total_debt", "cash_equivalents", "total_assets",
            "reserves_surplus", "share_capital", "borrowings_non_current",
            "borrowings_current", "total_current_assets",
            "total_current_liabilities", "inventory", "trade_receivables",
            "trade_payables", "ppe_net", "net_worth")


# balance-sheet line items — these are INSTANT concepts; every other mapped
# element is a duration (P&L / cash-flow) concept
_BS_ELEMENTS = frozenset((
    "EquityShareCapital", "Capital", "OtherEquity", "ReservesAndSurplus",
    "Equity", "EquityAttributableToOwnersOfParent", "NonControllingInterest",
    "BorrowingsNoncurrent", "LongTermBorrowings", "BorrowingsCurrent",
    "ShortTermBorrowings", "DeferredTaxLiabilitiesNet", "DeferredTaxAssetsNet",
    "PropertyPlantAndEquipment", "FixedAssets", "InvestmentProperty",
    "CapitalWorkInProgress", "Goodwill", "OtherIntangibleAssets",
    "IntangibleAssetsUnderDevelopment", "NoncurrentInvestments",
    "InvestmentsAccountedForUsingEquityMethod", "Investments",
    "CurrentInvestments", "LoansNoncurrent", "LoansCurrent", "Advances",
    "Inventories", "TradeReceivablesCurrent", "TradeReceivablesNoncurrent",
    "TradeReceivables", "CashAndCashEquivalents",
    "BankBalanceOtherThanCashAndCashEquivalents",
    "CashAndBalancesWithReserveBankOfIndia",
    "BalancesWithBanksAndMoneyAtCallAndShortNotice", "OtherCurrentAssets",
    "CurrentTaxAssets", "OtherCurrentLiabilities", "CurrentTaxLiabilities",
    "TradePayablesCurrent", "TradePayablesNoncurrent", "ProvisionsCurrent",
    "ProvisionsNoncurrent", "NoncurrentAssets", "CurrentAssets", "Assets",
    "EquityAndLiabilities", "CapitalAndLiabilities", "Liabilities",
    "CurrentLiabilities", "CurrentLiabilitiesAndProvisions",
))

_HEADER_DATE_ELEMENTS = ("DateOfStartOfReportingPeriod",
                         "DateOfEndOfReportingPeriod")


# ---- low-level parsing ----------------------------------------------------------

def fact_number(text: str | None, sign: str | None = None,
                scale: str | None = None) -> float | None:
    """Numeric value of an XBRL fact, honoring iXBRL-style sign/scale
    attributes defensively (plain XBRL facts carry neither)."""
    t = (text or "").strip().replace(",", "")
    if not t or t in ("-", "--"):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    if sign == "-":
        v = -v
    if scale:
        try:
            v *= 10.0 ** int(scale)
        except ValueError:
            pass
    return v


def duration_kind(start: _date, end: _date) -> str | None:
    """Q / H1 / 9M / FY from a duration's length (~3/6/9/12 months)."""
    days = (end - start).days
    if start >= end:
        return None
    if days <= 120:
        return "Q"
    if days <= 215:
        return "H1"
    if days <= 310:
        return "9M"
    return "FY"


def parse_filing(path: Path) -> dict:
    """One XBRL file -> {"header": {...}, "periods": {(cls,key): {elem: val}}}.

    cls is 'I' (instant at ISO date) or 'D' (duration, key f"{kind}|{end}").

    Column-period resolution (two passes) — NSE filings frequently carry
    MIS-DATED contexts where the cumulative column's <xbrli:context> repeats
    the quarter's startDate/endDate; the per-column header facts
    DateOfStart/EndOfReportingPeriod are authoritative and override the
    context definition. Facts referencing contexts that the file never
    defines (an early-era filer bug) fall back to those headers plus
    first-appearance order: unknown duration column #0 is the current
    discrete window, later ones run from FY start to the reporting end;
    unknown instant columns resolve to the reporting end / prior FY end.
    """
    root = ET.parse(path).getroot()

    contexts: dict[str, dict] = {}
    for ctx in root.findall(f"{{{XBRLI_NS}}}context"):
        cid = ctx.get("id")
        if not cid:
            continue
        inst = ctx.find(f"{{{XBRLI_NS}}}period/{{{XBRLI_NS}}}instant")
        start = ctx.find(f"{{{XBRLI_NS}}}period/{{{XBRLI_NS}}}startDate")
        end = ctx.find(f"{{{XBRLI_NS}}}period/{{{XBRLI_NS}}}endDate")
        entry: dict = {"dimensioned": ctx.find(f"{{{XBRLI_NS}}}scenario") is not None}
        if inst is not None and len((inst.text or "").strip()) >= 10:
            entry["instant"] = (inst.text or "").strip()[:10]
        elif start is not None and end is not None:
            entry["start"] = (start.text or "").strip()[:10]
            entry["end"] = (end.text or "").strip()[:10]
        contexts[cid] = entry

    # pass A — headers per contextRef + document-level info facts + facts
    col_dates: dict[str, list[str]] = {}
    header: dict[str, str] = {}
    face_value: float | None = None
    cols: dict[str, dict] = defaultdict(lambda: {"D": [], "I": []})
    order = 0
    for el in root:
        tag = el.tag
        if not tag.startswith("{") or tag.startswith(f"{{{XBRLI_NS}}}"):
            continue
        name = tag.rsplit("}", 1)[-1]
        text = (el.text or "").strip()
        cid = el.get("contextRef") or ""
        if name in _INFO_ELEMENTS and cid:
            header.setdefault(name, text[:200])
            if name in _HEADER_DATE_ELEMENTS and len(text) >= 10:
                col_dates.setdefault(cid, []).append(text[:10])
            continue
        hit = _ELEMENT_MAP.get(name)
        if hit is None or not text or not cid:
            continue
        unit = (el.get("unitRef") or "").strip()
        if unit in SKIP_UNITS:
            continue
        val = fact_number(text, el.get("sign"), el.get("scale"))
        if val is None:
            continue
        canon = hit[0]
        if canon == "face_value":
            face_value = val
            continue
        if canon not in _PER_SHARE_CANONS and unit in ("", "INR"):
            val /= INR_TO_CRORE                      # absolute INR -> crore
        cls = "I" if name in _BS_ELEMENTS else "D"
        cols[cid][cls].append((order, name, val))
        order += 1

    def iso_ok(s: str) -> bool:
        try:
            _date.fromisoformat(s)
            return True
        except ValueError:
            return False

    hdr_pairs: dict[str, tuple[str, str]] = {}
    for cid, dates in col_dates.items():
        if len(dates) >= 2 and iso_ok(dates[0]) and iso_ok(dates[1]):
            hdr_pairs[cid] = (dates[0], dates[1])
    file_window = hdr_pairs.get("OneD") or next(iter(hdr_pairs.values()), None)
    file_start, file_end = file_window if file_window else (None, None)
    fy_start = header.get("DateOfStartOfFinancialYear", "")[:10]
    prior_fy_end = ""
    if iso_ok(fy_start):
        y, m, d = (int(x) for x in fy_start.split("-"))
        prior_fy_end = (_date(y, m, d) - timedelta(days=1)).isoformat()

    periods: dict[tuple[str, str], dict[str, float]] = {}

    def emit(kind_key: tuple[str, str], rank: int, facts) -> None:
        if not facts:
            return
        bucket = periods.setdefault(kind_key, {})
        cur_rank = bucket.get("_rank", -1)
        if rank < cur_rank:
            return
        if rank > cur_rank:
            bucket.clear()
        bucket["_rank"] = rank
        for _order, name, val in facts:
            bucket.setdefault(name, val)          # first wins within a bucket

    resolved: dict[str, tuple] = {}
    unknown_durs: list[str] = []
    unknown_insts: list[str] = []
    for cid, groups in cols.items():
        cinfo = contexts.get(cid)
        if cinfo is not None and cinfo.get("dimensioned"):
            continue                              # segment/expense split axis
        if cinfo and "instant" in cinfo:
            resolved[cid] = ("I", cinfo["instant"], 3)
            continue
        pair = hdr_pairs.get(cid)
        rank = 3
        if not pair and cinfo and cinfo.get("start"):
            pair = (cinfo["start"], cinfo["end"])
            rank = 2
        if pair and iso_ok(pair[0]) and iso_ok(pair[1]):
            resolved[cid] = ("D", pair[0], pair[1], rank)
        else:
            if groups["I"]:
                unknown_insts.append(cid)
            if groups["D"]:
                unknown_durs.append(cid)

    for cid, res in resolved.items():
        if res[0] == "I":
            emit(("I", res[1]), res[2], cols[cid]["I"])
            continue
        kind = duration_kind(_date.fromisoformat(res[1]),
                             _date.fromisoformat(res[2]))
        if kind:
            emit(("D", f"{kind}|{res[2]}"), res[3], cols[cid]["D"])
    # undefined-context fallbacks (early-era filer bug): column order maps
    # #0 to the current discrete window and later ones to FY-to-date
    for i, cid in enumerate(unknown_durs):
        if i == 0 and file_window:
            s, e = file_start, file_end
        elif i > 0 and iso_ok(fy_start) and file_end:
            s, e = fy_start, file_end
        else:
            continue
        kind = duration_kind(_date.fromisoformat(s), _date.fromisoformat(e))
        if kind:
            emit(("D", f"{kind}|{e}"), 1, cols[cid]["D"])
    inst_targets = [t for t in (file_end, prior_fy_end) if t]
    for i, cid in enumerate(unknown_insts):
        if i < len(inst_targets):
            emit(("I", inst_targets[i]), 1, cols[cid]["I"])

    for bucket in periods.values():
        bucket.pop("_rank", None)
    return {"header": header, "face_value": face_value, "periods": periods}


def _iso_tuple(iso: str) -> tuple[int, int, int]:
    y, m, d = (int(x) for x in iso.split("-"))
    return y, m, d


def reduce_period(elems: dict[str, float]) -> dict[str, float]:
    """Tagged elements of one period -> canonical values (priority winner
    per canon; sum-groups totalled)."""
    best: dict[str, tuple[int, float]] = {}
    groups: dict[str, float] = defaultdict(float)
    for name, val in elems.items():
        hit = _ELEMENT_MAP.get(name)
        if hit is None:
            continue
        canon, prio = hit
        if canon in _SUM_GROUPS:
            groups[canon] += val
        elif canon not in best or prio > best[canon][0]:
            best[canon] = (prio, val)
    out = {c: v for c, (_p, v) in best.items()}
    out.update(groups)
    return out


# ---- selection across filings ----------------------------------------------------

def filing_ts(row: dict) -> str:
    """Sortable filing timestamp ('DD-MMM-YYYY HH:MM' -> 'YYYY-MM-DD HH:MM')."""
    raw = (row.get("filingDate") or "").strip()
    if not raw:
        return ""
    parts = raw.split(None, 1)
    dk = date_key(parts[0])
    return f"{dk} {parts[1]}" if len(parts) > 1 else dk


def filing_preference(row: dict, header: dict) -> tuple[int, int]:
    audited = ((header.get("WhetherResultsAreAuditedOrUnaudited")
                or row.get("audited") or "").lower().startswith("audited"))
    nature = (header.get("NatureOfReportStandaloneConsolidated")
              or row.get("consolidated") or "").lower()
    consolidated = nature.startswith("consol") and "non" not in nature
    return (0 if audited else 1, 0 if consolidated else 1)


def select_facts(parsed: list[tuple[dict, dict]]) -> dict[tuple, dict]:
    """Best-first first-wins merge across a symbol's filings.

    parsed: [(metadata_row, parse_filing(path))] already ordered so that
    earlier entries win ties (caller applies filing_preference then
    filingDate descending). Returns {(section,kind,date_iso): {canon: val,
    '_fv': fv}} where kind/date follow the pipeline conventions (balance
    sheets ride Q/FY records, cash flows their cumulative durations).
    """
    selected: dict[tuple, dict] = {}
    section = "standalone"

    def put(kind: str, diso: str, vals: dict[str, float]) -> None:
        rec = selected.setdefault((section, kind, diso), {})
        for canon, val in vals.items():
            if canon not in rec:
                rec[canon] = val
        if fv and "_fv" not in rec:
            rec["_fv"] = fv

    for row, ff in parsed:
        header, fv = ff["header"], ff.get("face_value")
        _aud, con = filing_preference(row, header)
        # con is a preference RANK (0 = consolidated, 1 = standalone)
        section = "consolidated" if con == 0 else "standalone"
        dur_kinds: dict[str, set[str]] = defaultdict(set)
        for cls, key in ff["periods"]:
            if cls == "D":
                kind, diso = key.split("|", 1)
                dur_kinds[diso].add(kind)
        for (cls, key), elems in sorted(ff["periods"].items()):
            vals = reduce_period(elems)
            if not vals:
                continue
            if cls == "D":
                kind, diso = key.split("|", 1)
                put(kind, diso, vals)
                continue
            # balance-sheet instant: ride the Q/FY record ending on this
            # date so chain subtraction never produces BS deltas
            diso = key
            m = _iso_tuple(diso)[1]
            if "FY" in dur_kinds.get(diso, ()):
                kind = "FY"
            elif "Q" in dur_kinds.get(diso, ()):
                kind = "Q"
            else:
                kind = "FY" if m == 3 else "Q"
            if kind not in ("Q", "FY"):
                continue          # BS must never pollute H1/9M chains
            put(kind, diso, vals)
    return selected


def build_raw_rows(selected: dict[tuple, dict]) -> list[dict]:
    """Selected canonical values -> internal rows consumed by
    financial_statements.build_section_records."""
    rows: list[dict] = []
    for (section, kind, diso), payload in sorted(selected.items()):
        fv = payload.pop("_fv", None)
        y, m, d = _iso_tuple(diso)
        for canon, val in payload.items():
            rows.append({"section": section, "label_raw": canon,
                         "canon": canon, "match_score": 1.0, "exact": True,
                         "face_value": fv, "page": 0,
                         "values": {f"{kind}|{y}-{m}-{d}": round(float(val), 4)}})
    return rows


# ---- document assembly -----------------------------------------------------------

def latest_balance_sheet(annuals: list[dict],
                         quarters: list[dict]) -> dict | None:
    for pool in (annuals, quarters):
        for rec in reversed(pool or []):
            bs = {k: rec[k] for k in _BS_KEYS
                  if isinstance(rec.get(k), (int, float))}
            if bs.get("total_equity") is not None or bs.get("total_debt"):
                return bs
    return None


def build_document(symbol: str, isin: str, name: str, sources: list[dict],
                   selected: dict[tuple, dict]) -> dict | None:
    """Selected facts -> final stock_financials document (process_stock
    shape) or None when nothing usable was extracted."""
    rows = build_raw_rows(selected)
    if not rows:
        return None
    sections = build_section_records(rows)
    doc: dict = {
        "schema_version": ss.SCHEMA_VERSION,
        "isin": isin, "symbol": symbol, "name": name,
        "fetched_at": now_iso(),
        "sources": sources,
    }
    total_issues: list[str] = []
    confs: list[int] = []
    wrote = False
    for section_name in ("consolidated", "standalone"):
        smap = sections.get(section_name)
        if not smap:
            continue
        quarters, annuals = assemble(smap)
        if not quarters and not annuals:
            continue
        issues, conf = validate_and_score(quarters, annuals)
        total_issues.extend(f"{section_name}:{i}" for i in issues)
        confs.append(conf)
        ttm = compute_ttm(quarters)
        block: dict = {"quarters": quarters, "annual": annuals, "ttm": ttm}
        bs = latest_balance_sheet(annuals, quarters)
        if bs:
            block["latest_balance_sheet"] = bs
        if ttm:
            for rec in reversed(quarters + annuals):
                eps = rec.get("eps_basic")
                if isinstance(eps, (int, float)):
                    ttm.setdefault("eps", eps)     # db.fundamental_snapshot alias
                    break
        doc[section_name] = block
        wrote = True
    if not wrote:
        return None
    primary = doc.get("consolidated") or doc.get("standalone") or {}
    doc["_latest_balance_sheet"] = primary.get("latest_balance_sheet")
    overall = max(confs) if confs else 0
    doc["validation"] = {"issues": total_issues,
                         "confidence": max(0, min(100, overall)),
                         "checked_at": now_iso()}
    return doc


# ---- comparison vs existing (PDF-pilot) outputs ----------------------------------

_COMPARE_KEYS = (
    "revenue_from_operations", "total_income", "employee_benefits",
    "finance_costs", "depreciation_amortisation", "other_expenses",
    "total_expenses", "pbt", "tax_expense", "pat", "ebitda",
    "share_capital", "reserves_surplus", "total_equity", "total_assets",
    "total_debt", "cash_equivalents", "cfo", "capex",
)


def diff_documents(old: dict, new: dict, tol: float = 0.05,
                   cap: int = 40) -> list[dict]:
    """Per-period relative discrepancies (>tol) between two documents on
    their preferred sections' quarter+annual records."""
    def block(doc):
        b = doc.get("consolidated") or doc.get("standalone") or {}
        idx: dict[str, dict] = {}
        for rec in (b.get("quarters") or []) + (b.get("annual") or []):
            pe = rec.get("period_end")
            if pe:
                idx.setdefault(pe, {}).update(
                    {k: v for k, v in rec.items()
                     if k in _COMPARE_KEYS and isinstance(v, (int, float))})
        return idx

    old_idx, new_idx = block(old), block(new)
    out: list[dict] = []
    for pe in sorted(set(old_idx) & set(new_idx)):
        for k in _COMPARE_KEYS:
            o, n = old_idx[pe].get(k), new_idx[pe].get(k)
            if o is None or n is None or abs(o) < 1e-9:
                continue
            rel = (n - o) / abs(o)
            if abs(rel) > tol:
                out.append({"period_end": pe, "key": k, "old_cr": round(o, 2),
                            "new_cr": round(n, 2), "rel_diff": round(rel, 4)})
                if len(out) >= cap:
                    return out
    return out


def load_existing(isin: str, out_dir: Path) -> dict | None:
    path = Path(out_dir) / f"{isin}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---- orchestration ---------------------------------------------------------------

def load_metadata(meta_path: Path | None = None) -> dict[str, list[dict]]:
    """_metadata.json rows grouped by uppercase symbol."""
    meta_path = Path(meta_path) if meta_path else XBRL_META_PATH
    by_sym: dict[str, list[dict]] = defaultdict(list)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for row in meta.values():
        sym = (row.get("symbol") or "").upper().strip()
        if sym:
            by_sym[sym].append(row)
    return by_sym


def prepare_symbol(symbol: str, isin: str, name: str, rows: list[dict],
                   raw_dir: Path) -> tuple[dict | None, dict]:
    """Parse a symbol's filings, apply the selection policy and assemble the
    final document WITHOUT writing anything. Returns (doc|None, stats)."""
    stats: dict = {"files_seen": len(rows)}
    filings: list[tuple[dict, Path]] = []
    missing = failed = 0
    for row in rows:
        seq = row.get("seqNumber") or ""
        path = Path(raw_dir) / symbol / f"{symbol}_{seq}.xml"
        if not path.exists():
            missing += 1
            continue
        filings.append((row, path))
    stats["files_missing"] = missing
    parsed: list[tuple[dict, dict, Path]] = []
    for row, path in filings:
        try:
            ff = parse_filing(path)
        except Exception as exc:                      # one bad file != dead run
            failed += 1
            log.warning("skip %s: %s", path.name, exc)
            continue
        if any(n in _ELEMENT_MAP for p in ff["periods"].values() for n in p):
            parsed.append((row, ff, path))
    stats.update({"parse_failed": failed, "parsed_ok": len(parsed)})
    if not parsed:
        stats["status"] = "parse_failed" if failed else "no_canonical_match"
        return None, stats
    parsed.sort(key=lambda r: filing_ts(r[0]), reverse=True)
    parsed.sort(key=lambda r: filing_preference(r[0], r[1]["header"]))
    selected = select_facts([(row, ff) for row, ff, _p in parsed])
    sources = [{"url": row.get("xbrl") or "", "date": row.get("filingDate"),
                "sha256": sha256_file(path), "tier": "xbrl"}
               for row, _ff, path in parsed]
    doc = build_document(symbol, isin, name, sources, selected)
    if doc is None:
        stats["status"] = "no_statements"
    return doc, stats


def fill_symbol(symbol: str, isin: str, name: str, rows: list[dict],
                raw_dir: Path, out_dir: Path, dry_run: bool = False
                ) -> dict:
    """Parse + select + write one symbol; returns its status summary."""
    summary: dict = {"isin": isin, "symbol": symbol}
    doc, stats = prepare_symbol(symbol, isin, name, rows, raw_dir)
    summary.update(stats)
    if doc is None:
        return summary
    old = load_existing(isin, out_dir)
    if old:
        diffs = diff_documents(old, doc)
        if diffs:
            summary["diffs_vs_existing"] = diffs
            log.info("%s/%s: %d discrepancy(ies) >5%% vs existing file",
                     symbol, isin, len(diffs))
    primary = doc.get("consolidated") or doc.get("standalone") or {}
    summary["quarters"] = len(primary.get("quarters") or [])
    summary["annual"] = len(primary.get("annual") or [])
    summary["confidence"] = doc["validation"]["confidence"]
    if not dry_run:
        save_json(Path(out_dir) / f"{isin}.json", doc)
        summary["status"] = "ok"
    else:
        summary["status"] = "ok(dry)"
    return summary


def run(symbols: list[str] | None = None, limit: int | None = None,
        dry_run: bool = False, meta_path: Path | None = None,
        raw_dir: Path | None = None, out_dir: Path | None = None,
        identity: dict | None = None) -> dict:
    """Pipeline over the requested universe; returns run-level counters."""
    raw_dir = Path(raw_dir) if raw_dir else XBRL_RAW_DIR
    out_dir = Path(out_dir) if out_dir else FINANCIALS_DIR
    by_sym = load_metadata(meta_path)
    identity = identity if identity is not None else load_identity()
    sym2isin: dict[str, str] = {}
    for isin, irow in identity.items():
        s = ((irow or {}).get("symbol") or "").upper().strip()
        if s:
            sym2isin.setdefault(s, isin)
    universe = sorted(set(by_sym) & set(sym2isin)) if symbols is None else \
        [s.strip().upper() for s in symbols]
    universe = [s for s in universe if s]
    if limit is not None:
        universe = universe[:max(0, limit)]
    log.info("xbrl_fill: %d symbol(s) in scope (dry_run=%s)",
             len(universe), dry_run)
    summary: dict = {
        "symbols_in_scope": len(universe),
        "symbols_written": 0, "symbols_failed": 0, "parsed_ok": 0,
        "parse_failed": 0, "no_canonical_match": 0, "files_missing": 0,
        "dry_run": bool(dry_run), "per_symbol": {},
    }
    for sym in universe:
        isin = sym2isin.get(sym)
        if not isin:
            summary["symbols_failed"] += 1
            summary["per_symbol"][sym] = {"status": "no_identity_isin"}
            continue
        name = (identity.get(isin) or {}).get("name") or ""
        try:
            st = fill_symbol(sym, isin, name, by_sym.get(sym) or [],
                             raw_dir, out_dir, dry_run=dry_run)
        except Exception as exc:                      # never kill the run
            log.exception("fill failed for %s", sym)
            st = {"isin": isin, "symbol": sym, "status": "error",
                  "error": str(exc)}
        summary["per_symbol"][sym] = st
        if st.get("status") in ("ok", "ok(dry)"):
            summary["symbols_written"] += 1
        elif st.get("status") == "no_canonical_match":
            summary["no_canonical_match"] += 1
        else:
            summary["symbols_failed"] += 1
        summary["parsed_ok"] += st.get("parsed_ok", 0)
        summary["parse_failed"] += st.get("parse_failed", 0)
        summary["files_missing"] += st.get("files_missing", 0)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m src.xbrl_fill",
        description="Convert NSE financial-results XBRL filings into "
                    "data/stock_financials/<ISIN>.json")
    ap.add_argument("--symbols", help="comma-separated NSE symbols "
                                      "(default: all metadata ∩ identity)")
    ap.add_argument("--limit", type=int, help="process at most N symbols")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, write nothing")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    syms = [s.strip() for s in args.symbols.split(",")] if args.symbols \
        else None
    summary = run(symbols=syms, limit=args.limit, dry_run=args.dry_run)
    print(json.dumps({k: v for k, v in summary.items() if k != "per_symbol"},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
