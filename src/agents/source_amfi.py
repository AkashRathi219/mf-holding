"""Channel 3 ``amfi`` - the AMFI source agent (SPEC §11.2 source agents,
§11.3 channel 3, AC-17, PLAN T23).

For an escalated ticket ``(amc, scheme, month)`` whose AMC-site channels did
not yield a complete portfolio, this channel consults AMFI's official
scheme-wise portfolio disclosure and fills the gap.  AMFI is merge-priority 0
in the webapp (``webapp/db.py::_SOURCE_PRIORITY``: amfi 0 > amc_website 1 >
advisorkhoj 2 > index 3), so a complete AMFI find WINS the merge and lifts the
displayed coverage into T0 on the next audit.  ``data/parsed/amfi/`` is empty
today (0 files) while ``data/parsed/advisorkhoj`` has 46 - this agent starts
on that backlog and treats "nothing found" as a normal, reportable outcome:
it NEVER fabricates holdings.

Endpoints (all derived from the working amfiindia.com access patterns in
``src/amfi_otherdata.py`` - same base URL, same Next.js page-payload trick,
same API params; no undocumented endpoint is invented):

1. ``/otherdata/scheme-wise-disclosure`` - the page payload embeds the MF
   directory (``mf_id``/``mf_name`` pairs); the ticket's AMC is resolved to an
   AMFI ``MF_ID`` with the exact regex ``amfi_otherdata.mutual_funds`` uses
   (after the same ``_rsc_unescape`` treatment of the Next.js chunks).
2. ``/api/schemewisedisclosure-investment`` - SEBI scheme-wise disclosure rows
   for ``MF_ID`` + ``strMonth`` (quarter START as ``dd-MMM-yyyy`` Title-case,
   the exact param format ``amfi_otherdata.scheme_wise_disclosure``
   documents); the only consulted endpoint whose payload carries portfolio
   holding rows (``Scheme_Name``/``Company_Name``/``ISIN``/``MarketValue``/
   ``MarketValuePercentage``/``Security_Type``/``QuarterDate`` per row, the
   shape saved under ``data/raw/amfi_otherdata/disclosure/``).

The HTTP client is INJECTED (an object exposing ``.get(url, **kw)``); with no
client the channel reports ``skipped_no_client`` and the ladder proceeds - no
real call is ever made from tests or unconfigured runs.  Parsing converts a
raw payload into the exact on-disk shape ``webapp/db.py::_load_amfi_schemes``
expects: ``{amc, as_of, schemes: {fund: {holdings: [{company, isin,
percent_nav, market_value, sector, section}]}}}``.

Write discipline: a parsed record is persisted ONLY when it reaches tier T0
``COMPLETE_100`` (strict full-disclosure rule, Σ >= 95).  An incomplete AMFI
record must never be written: at merge priority 0 it would outrank a complete
``amc_website`` parse and REGRESS the displayed coverage.  Writes are
append-only and atomic (temp file + ``os.replace``) and land FLAT directly
under ``<out_dir>/`` as ``<amc_slug>_<YYYY-MM>_<digest>.json`` - the flat
top-level glob in ``webapp/db.py::_load_amfi_schemes`` is the loader contract
(a nested tree would be invisible to the merge); an existing file is returned
untouched, never overwritten.

The record's ``document_class`` is stamped from
``src.document_class.classify`` before the write; the channel's source label
carries no class token, so AMFI records stamp as ``unknown`` and are judged by
the STRICT full-disclosure rule - the safe default that can never turn a
partial find into an accepted top-10.

Escalation citizenship (OQ-7): the channel is a pure worker - it never
mutates the escalation queue or the DB, reports one :class:`ChannelResult`,
maps every exception through ``src.agents.taxonomy.classify_exception`` (a
failed result never carries ``failure_code=None``) and never evades a block.
A factsheet/top-N document does NOT close the ticket
(``ERR_HOLDINGS_TOPN_ONLY``).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from src.agents import taxonomy
from src.agents.channels import (
    ChannelResult,
    _name_matches,
    evaluate_candidate_payload,
)
from src.agents.escalation import Ticket
from src.document_class import classify as classify_document

CHANNEL = "amfi"

BASE_URL = "https://www.amfiindia.com"
AMFI_PARSED_DIR = Path("data") / "parsed" / "amfi"
# LAYOUT CONTRACT: the webapp loader ``webapp/db.py::_load_amfi_schemes`` reads
# this directory with a FLAT top-level glob (``AMFI_DIR.glob("*.json")`` - no
# rglob, no nesting), so write_parsed persists records DIRECTLY under out_dir/
# as ``<amc_slug>_<YYYY-MM>_<digest>.json``.  A nested ``<AMC>/<YYYY>/<MM>/``
# tree would be invisible to the merge (AMFI is merge-priority 0) - do not nest.

DISCLOSURE_PAGE_PATH = "/otherdata/scheme-wise-disclosure"
DISCLOSURE_API_PATH = "/api/schemewisedisclosure-investment"

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

WAF_CODE = "ERR_WAF_CLOUDFLARE_1015"
CODE_SUSPECT_SCALE = "ERR_SCALE_SUSPECT_WEIGHTS"
CODE_INCOMPLETE_SUM = "ERR_HOLDINGS_INCOMPLETE_SUM"
CODE_TOPN_ONLY = "ERR_HOLDINGS_TOPN_ONLY"
CODE_PARSER_PARTIAL = "ERR_HOLDINGS_PARSER_PARTIAL"
CODE_MISSING = "ERR_SCHEME_MISSING_IN_DB"

_CHANNEL_CODES = (
    WAF_CODE,
    CODE_SUSPECT_SCALE,
    CODE_INCOMPLETE_SUM,
    CODE_TOPN_ONLY,
    CODE_PARSER_PARTIAL,
    CODE_MISSING,
)
for _code in _CHANNEL_CODES:
    if not taxonomy.is_valid_code(_code):
        raise RuntimeError(f"source_amfi references unknown taxonomy code {_code!r}")

# Outcome codes ranked most-informative first; the walk reports the
# highest-ranked outcome it observed (ties keep the first occurrence).
_FAILURE_PRIORITY: dict[str, int] = {
    CODE_SUSPECT_SCALE: 4,
    CODE_INCOMPLETE_SUM: 3,
    CODE_TOPN_ONLY: 2,
    CODE_PARSER_PARTIAL: 1,
    CODE_MISSING: 0,
}

REASON_SKIPPED_NO_CLIENT = "skipped_no_client"

PORTFOLIO_STRATEGY = "scheme_wise_disclosure_rows"
PORTFOLIO_SOURCE_LABEL = "amfi:scheme_wise_disclosure"


# ---------------------------------------------------------------------------
# Fetch targets (derived from src/amfi_otherdata.py - nothing invented)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchTarget:
    """One AMFI endpoint the channel consults (path joined onto ``base_url``)."""

    key: str
    path: str
    role: str
    description: str


FETCH_TARGETS: tuple[FetchTarget, ...] = (
    FetchTarget(
        key="mf_directory",
        path=DISCLOSURE_PAGE_PATH,
        role="resolve_mf_id",
        description=(
            "scheme-wise-disclosure page payload; mf_id/mf_name pairs are extracted "
            "with the src/amfi_otherdata.mutual_funds regex to resolve the ticket's "
            "AMC to an AMFI MF_ID"
        ),
    ),
    FetchTarget(
        key="scheme_wise_disclosure",
        path=DISCLOSURE_API_PATH,
        role="portfolio_rows",
        description=(
            "SEBI scheme-wise disclosure rows (MF_ID + strMonth=quarter-start "
            "dd-MMM-yyyy Title-case); the only consulted endpoint whose payload "
            "carries portfolio holding rows"
        ),
    ),
)

PORTFOLIO_URLS: tuple[str, ...] = tuple(
    f"{BASE_URL}{target.path}" for target in FETCH_TARGETS
)


# ---------------------------------------------------------------------------
# Low-level helpers (mirror the src/amfi_otherdata.py access patterns)
# ---------------------------------------------------------------------------

_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_MF_PAIR_RE = re.compile(r'"mf_id":"?(\d+)"?,"mf_name":"([^"]+)"')
_MONTH_RE = re.compile(r"(\d{4})[-/](\d{1,2})")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_YM_DATE_RE = re.compile(r"\d{4}-\d{2}")
_DDMMM_DATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")


def _rsc_unescape(raw: str) -> str:
    """Undo escaping inside Next.js ``self.__next_f.push([1,"..."])`` chunks
    (same treatment as ``src/amfi_otherdata._rsc_unescape``)."""
    return (raw.replace('\\"', '"').replace("\\\\", "\\")
               .replace("\\u0026", "&").replace("\\/", "/"))


def _resp_text(resp: object) -> str:
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        return text
    return str(resp)


def _resp_json(resp: object) -> object:
    json_fn = getattr(resp, "json", None)
    if callable(json_fn):
        return json_fn()
    return json.loads(_resp_text(resp))


def _headers(base_url: str, referer_path: str = "") -> dict[str, str]:
    headers = dict(UA)
    if referer_path:
        headers["Referer"] = f"{base_url}{referer_path}"
    return headers


def _quarter_start_token(month: object) -> str:
    """AMFI ``strMonth`` for the quarter containing ``month``: the quarter
    START as ``dd-MMM-yyyy`` Title-case, the exact param format
    ``amfi_otherdata.scheme_wise_disclosure`` documents (e.g. 01-Jul-2026)."""
    match = _MONTH_RE.search(str(month or ""))
    if not match:
        return ""
    year, mon = int(match.group(1)), int(match.group(2))
    if not 1 <= mon <= 12:
        return ""
    q_month = ((mon - 1) // 3) * 3 + 1
    return f"01-{_MON[q_month - 1]}-{year}"


def _resolve_mf_id(amc: object, client: object, base_url: str) -> str:
    """AMFI ``MF_ID`` for ``amc`` from the disclosure page payload, resolved
    with the same page scrape + regex ``amfi_otherdata.mutual_funds`` uses.

    Returns ``""`` when the AMC is not in the directory (graceful
    nothing-found); transport failures propagate to the caller's taxonomy
    mapping (a block must be reported, never swallowed).
    """
    resp = client.get(
        f"{base_url}{DISCLOSURE_PAGE_PATH}",
        headers=_headers(base_url),
    )
    text = _rsc_unescape(_resp_text(resp))
    names: dict[int, str] = {}
    for mf_id, mf_name in _MF_PAIR_RE.findall(text):
        names.setdefault(int(mf_id), mf_name)
    for mf_id in sorted(names):
        if _name_matches(names[mf_id], str(amc or "")):
            return str(mf_id)
    return ""


# ---------------------------------------------------------------------------
# Fetch + parse + persist
# ---------------------------------------------------------------------------


def fetch_candidates(
    ticket: Ticket,
    *,
    client: object = None,
    base_url: str = BASE_URL,
) -> list[dict]:
    """Raw AMFI payloads that may carry the ticket's portfolio rows.

    Consults :data:`FETCH_TARGETS` in order: the disclosure page resolves the
    ticket's AMC to an ``MF_ID`` (no match -> ``[]``, graceful nothing-found),
    then the scheme-wise-disclosure API is queried for the quarter containing
    ``ticket.month`` (the source's own granularity).  ``client`` MUST be
    injected (an object exposing ``.get(url, **kw)``); with ``client=None``
    this returns ``[]`` without any network call and :func:`run` reports
    ``skipped_no_client``.  Raw payloads are returned as received (list or
    dict); :func:`parse_candidate` decides recognisability.
    """
    if client is None:
        return []
    mf_id = _resolve_mf_id(ticket.amc, client, base_url)
    if not mf_id:
        return []
    str_month = _quarter_start_token(ticket.month)
    if not str_month:
        return []
    resp = client.get(
        f"{base_url}{DISCLOSURE_API_PATH}",
        params={"MF_ID": mf_id, "strMonth": str_month},
        headers=_headers(base_url, DISCLOSURE_PAGE_PATH),
    )
    payload = _resp_json(resp)
    if isinstance(payload, (dict, list)):
        return [payload]
    return []


_ROW_NAME_KEYS = ("Company_Name", "company", "stock_name", "name", "Security_Name")
_ROW_WEIGHT_KEYS = ("MarketValuePercentage", "percent_nav", "weight_pct", "pct_nav")
_ROW_ISIN_KEYS = ("ISIN", "isin")
_ROW_MV_KEYS = ("MarketValue", "market_value", "value")
_ROW_SECTOR_KEYS = ("Sector", "sector", "industry", "rating")
_ROW_SECTION_KEYS = ("Security_Type", "section", "instrument_type", "asset_class")
_ROW_SCHEME_KEYS = ("Scheme_Name", "scheme_name", "fund_name", "scheme")
_PAYLOAD_AMC_KEYS = ("amc", "amc_name", "mf_name", "mfName")
_PAYLOAD_DATE_KEYS = ("as_of", "date", "month", "QuarterDate")


def _first(row: Mapping, keys: tuple[str, ...]) -> object:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return ""


def _parse_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            text = text[:-1].strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _rows_of(payload: object) -> list[Mapping]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("rows", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
    return []


def _iso_date(payload: object, rows: list[Mapping]) -> str:
    """Disclosure as-of date carried by the payload (top-level keys first,
    then the rows' ``QuarterDate``/``date``); ``""`` when none is parseable."""
    candidates: list[object] = []
    if isinstance(payload, Mapping):
        candidates.extend(payload.get(key) for key in _PAYLOAD_DATE_KEYS)
    if rows:
        first = rows[0]
        if isinstance(first, Mapping):
            candidates.extend(
                first.get(key) for key in ("QuarterDate", "date", "as_of", "month")
            )
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        match = _ISO_DATE_RE.search(text)
        if match:
            return match.group(0)
        match = _DDMMM_DATE_RE.search(text)
        if match:
            day, mon, year = int(match.group(1)), match.group(2).title(), int(match.group(3))
            if mon in _MON:
                return f"{year}-{_MON.index(mon) + 1:02d}-{day:02d}"
        if _YM_DATE_RE.fullmatch(text):
            return text
    return ""


def parse_candidate(payload: object, *, source_file: str = "") -> dict | None:
    """Convert a raw AMFI payload into the ``_load_amfi_schemes`` record shape.

    Recognised payloads are the scheme-wise-disclosure responses: a bare list
    of rows, or ``{"status": "ok", "rows": [...]}`` / ``{"data": [...]}``,
    where each row carries a scheme name, a company/instrument name and a
    parseable %NAV weight (the real on-disk row shape).  Rows are grouped per
    scheme and mapped onto the loader's holding keys.  Anything else -
    ``{"message": "Nil"}``, directory lists, tracking/AUM rows - returns
    ``None`` (holdings are never fabricated).
    """
    rows = _rows_of(payload)
    if not rows:
        return None
    default_scheme = ""
    if isinstance(payload, Mapping):
        default_scheme = str(_first(payload, _ROW_SCHEME_KEYS) or "")
    schemes: dict[str, list[dict]] = {}
    for row in rows:
        company = str(_first(row, _ROW_NAME_KEYS) or "").strip()
        weight = _parse_float(_first(row, _ROW_WEIGHT_KEYS))
        if not company or weight is None:
            continue
        scheme = str(_first(row, _ROW_SCHEME_KEYS) or default_scheme or "").strip()
        if not scheme:
            continue
        market_value = _parse_float(_first(row, _ROW_MV_KEYS))
        scheme_rows = schemes.setdefault(scheme, {"holdings": []})["holdings"]
        scheme_rows.append({
            "company": company,
            "isin": str(_first(row, _ROW_ISIN_KEYS) or "").strip().upper(),
            "percent_nav": round(weight, 6),
            "market_value": market_value if market_value is not None else "",
            "sector": str(_first(row, _ROW_SECTOR_KEYS) or "").strip(),
            "section": str(_first(row, _ROW_SECTION_KEYS) or "").strip(),
        })
    if not schemes:
        return None
    amc = ""
    if isinstance(payload, Mapping):
        amc = str(_first(payload, _PAYLOAD_AMC_KEYS) or "").strip()
    return {
        "amc": amc,
        "as_of": _iso_date(payload, rows),
        "schemes": schemes,
        "source_file": str(source_file or ""),
    }


def write_parsed(record: Mapping, *, out_dir: str | Path = AMFI_PARSED_DIR) -> Path:
    """Atomically persist one parsed record; append-only, never overwrites.

    FLAT layout (loader contract): ``<out_dir>/<amc_slug>_<YYYY-MM>_<digest>.json``
    - the file sits DIRECTLY under ``out_dir`` because
    ``webapp/db.py::_load_amfi_schemes`` reads only a flat top-level
    ``AMFI_DIR.glob("*.json")``; a nested ``<AMC>/<YYYY>/<MM>/`` tree would be
    invisible to the merge (AMFI is merge-priority 0).  ``YYYY-MM`` comes from
    the record's ``as_of``; when ``as_of`` is absent the short content digest
    stands in for the stamp.  The digest is the sha256 of the canonical record
    JSON, so re-writing the SAME record resolves to the existing file and
    returns it untouched, while a genuinely different find lands in its own
    sibling file: nothing is ever overwritten or silently dropped.  The write
    itself is atomic (temp file + ``os.replace``).
    """
    amc_slug = re.sub(r"[^a-z0-9]+", "_", str(record.get("amc") or "").casefold()).strip("_") or "amc"
    digest = hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    match = _YM_DATE_RE.search(str(record.get("as_of") or ""))
    name = (
        f"{amc_slug}_{match.group(0)}_{digest}.json" if match
        else f"{amc_slug}_{digest}.json"
    )
    base = Path(out_dir)
    path = base / name
    if path.exists():
        return path
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f"{path.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    if path.exists():
        tmp.unlink()
        return path
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Channel entry point
# ---------------------------------------------------------------------------


def run(
    ticket: Ticket,
    *,
    client: object = None,
    out_dir: str | Path = AMFI_PARSED_DIR,
    parse: object = None,
    write: object = None,
    now: object = None,
) -> ChannelResult:
    """Work one escalated ticket through the AMFI source channel (§11.3 #3).

    Injected dependencies (no real network, no filesystem access of its own):

    * ``client`` - an object exposing ``.get(url, **kw)`` (e.g. an httpx
      client).  ``None`` short-circuits the channel with
      ``skipped_no_client`` so the ladder proceeds to the next channel; no
      real call is ever made without an injected client.
    * ``out_dir`` - destination directory for the parsed record (written FLAT
      directly under it, see :func:`write_parsed`; default
      :data:`AMFI_PARSED_DIR`).
    * ``parse`` / ``write`` - optional replacements for
      :func:`parse_candidate` / :func:`write_parsed` with the same signatures.
    * ``now`` is accepted for caller uniformity (episode stamping); the
      channel itself is clock-free.

    Success requires a full-disclosure-class record reaching Σ >= 95
    (``tiers.classify`` -> T0 ``COMPLETE_100``); only then is the record
    written (an incomplete priority-0 record would regress the merge) and its
    path reported in ``new_document_paths``.  A factsheet/top-N document does
    NOT close the ticket.  Every exception is mapped through
    ``taxonomy.classify_exception``; ``failure_code`` is never ``None`` on a
    failed run and the queue/DB are never mutated.
    """
    if client is None:
        return ChannelResult(
            channel=CHANNEL,
            success=False,
            reason=(
                f"{REASON_SKIPPED_NO_CLIENT}: no HTTP client injected; AMFI endpoints "
                f"not queried and the ladder proceeds to the next channel"
            ),
            new_document_paths=[],
            strategy_used="",
            failure_code=CODE_MISSING,
        )
    parse_fn = parse if parse is not None else parse_candidate
    write_fn = write if write is not None else write_parsed

    try:
        candidates = fetch_candidates(ticket, client=client)
    except Exception as exc:
        code = taxonomy.classify_exception(exc) or CODE_MISSING
        reason = f"AMFI fetch failed ({code}); no portfolio payload obtained"
        if code == WAF_CODE:
            reason += " - stopped without evading, rotating IPs or retrying"
        return ChannelResult(
            channel=CHANNEL,
            success=False,
            reason=reason,
            new_document_paths=[],
            strategy_used=PORTFOLIO_STRATEGY,
            failure_code=code,
        )

    if not candidates:
        return ChannelResult(
            channel=CHANNEL,
            success=False,
            reason=(
                "no portfolio payload obtained from the AMFI scheme-wise disclosure "
                "endpoints (AMC not in the AMFI directory or nothing published for "
                "the scheme-month); nothing fabricated"
            ),
            new_document_paths=[],
            strategy_used=PORTFOLIO_STRATEGY,
            failure_code=CODE_MISSING,
        )

    best: tuple[int, str, str] | None = None

    def note(code: str, reason: str) -> None:
        nonlocal best
        priority = _FAILURE_PRIORITY.get(code, -1)
        if best is None or priority > best[0]:
            best = (priority, code, reason)

    for candidate in candidates:
        try:
            record = parse_fn(candidate, source_file=PORTFOLIO_SOURCE_LABEL)
        except Exception as exc:
            code = taxonomy.classify_exception(exc) or CODE_PARSER_PARTIAL
            note(code, f"AMFI payload parse failed ({code})")
            continue
        if not isinstance(record, Mapping) or not record:
            note(
                CODE_MISSING,
                "AMFI payload is not a recognisable portfolio disclosure "
                "(no scheme-attributed holding rows); nothing fabricated",
            )
            continue

        closed = dict(record)
        closed["amc"] = str(closed.get("amc") or ticket.amc or "").strip()
        verdict = evaluate_candidate_payload(
            closed, ticket.scheme, source_file=PORTFOLIO_SOURCE_LABEL
        )

        if verdict.is_t0:
            closed["document_class"] = classify_document(
                closed, source_file=PORTFOLIO_SOURCE_LABEL
            )
            try:
                path = write_fn(closed, out_dir=out_dir)
            except Exception as exc:
                code = taxonomy.classify_exception(exc) or CODE_PARSER_PARTIAL
                note(code, f"complete AMFI find could not be written ({code})")
                continue
            return ChannelResult(
                channel=CHANNEL,
                success=True,
                reason=(
                    f"closed by AMFI {closed['document_class']} record "
                    f"(Σ={verdict.tier.coverage_pct:.2f}%): {verdict.tier.reason}; "
                    f"written to {Path(path).name}"
                ),
                new_document_paths=[str(path)],
                strategy_used=PORTFOLIO_STRATEGY,
                failure_code=None,
            )
        if verdict.failure_code == CODE_SUSPECT_SCALE:
            note(
                CODE_SUSPECT_SCALE,
                f"suspect scale in the AMFI payload: {verdict.tier.reason}",
            )
        elif verdict.failure_code == CODE_TOPN_ONLY:
            note(
                CODE_TOPN_ONLY,
                f"only a factsheet_topn document (Σ={verdict.tier.coverage_pct:.2f}%) "
                f"- accepted T1 elsewhere, does NOT close this ticket",
            )
        elif verdict.failure_code == CODE_INCOMPLETE_SUM:
            note(
                CODE_INCOMPLETE_SUM,
                f"incomplete {verdict.document_class} AMFI parse "
                f"(Σ={verdict.tier.coverage_pct:.2f}%): {verdict.tier.reason}; "
                f"not written (an incomplete priority-0 record would regress the merge)",
            )
        else:
            note(
                CODE_MISSING,
                f"scheme not present in the AMFI scheme-wise disclosure payload "
                f"({verdict.document_class})",
            )

    if best is not None:
        _, code, reason = best
        return ChannelResult(
            channel=CHANNEL,
            success=False,
            reason=reason,
            new_document_paths=[],
            strategy_used=PORTFOLIO_STRATEGY,
            failure_code=code,
        )
    return ChannelResult(
        channel=CHANNEL,
        success=False,
        reason="no AMFI candidate closed the ticket",
        new_document_paths=[],
        strategy_used=PORTFOLIO_STRATEGY,
        failure_code=CODE_MISSING,
    )


__all__ = [
    "AMFI_PARSED_DIR",
    "BASE_URL",
    "CHANNEL",
    "CODE_INCOMPLETE_SUM",
    "CODE_MISSING",
    "CODE_PARSER_PARTIAL",
    "CODE_SUSPECT_SCALE",
    "CODE_TOPN_ONLY",
    "DISCLOSURE_API_PATH",
    "DISCLOSURE_PAGE_PATH",
    "FETCH_TARGETS",
    "FetchTarget",
    "PORTFOLIO_SOURCE_LABEL",
    "PORTFOLIO_STRATEGY",
    "PORTFOLIO_URLS",
    "REASON_SKIPPED_NO_CLIENT",
    "UA",
    "WAF_CODE",
    "fetch_candidates",
    "parse_candidate",
    "run",
    "write_parsed",
]
