"""Channel 4 ``advisorkhoj`` - the Advisorkhoj source agent (SPEC §11.2 source
agents, §11.3 channel 4, AC-17, PLAN T24).

For an escalated ticket ``(amc, scheme, month)`` that the earlier channels did
not close, this channel consults Advisorkhoj - a third-party REPUBLISHER of the
AMCs' monthly portfolio disclosures.  Advisorkhoj is merge-priority 2 in the
webapp (``webapp/db.py::_SOURCE_PRIORITY``: amfi 0 > amc_website 1 >
advisorkhoj 2 > index 3), so it is lower-trust than AMFI and only wins the
merge when AMFI (and the AMC website) are absent; its numbers are never
preferred over a higher-priority complete source.  ``data/parsed/advisorkhoj/``
holds the existing corpus (46 flat per-AMC JSONs, e.g. ``360 ONE Mutual Fund
.json``) - this agent extends that backlog and treats "nothing found" as a
normal, reportable outcome: it NEVER fabricates holdings.

Access patterns (all derived from the verified Advisorkhoj download-centre
structure documented in ``archive/docs/ADVISORKHOJ_PLAN.md`` - no undocumented
endpoint is invented):

1. ``/form-download-centre/`` - any download-centre page embeds the AMC list
   (``<select id="select_company">``); the ticket's AMC is resolved to its
   Advisorkhoj URL slug (AMC-Name-With-Dashes).
2. ``/form-download-centre/Mutual/{slug}/Monthly-Portfolio-Disclosures`` - the
   category page lists the disclosure documents as ``<a class="blue_text">``
   links labelled ``Monthly Portfolio Disclosure - {Month Year}``; the links
   matching the ticket's month are followed and each linked document is
   fetched.  Advisorkhoj links usually point at AMC-hosted pages/files; this
   stdlib channel consumes a linked document only when its body parses as a
   JSON portfolio payload in the corpus's nested shape - binary XLSX/PDF/HTML
   bodies are skipped (parsing those belongs to the amc_website pipeline).

The HTTP client is INJECTED (an object exposing ``.get(url, **kw)``); with no
client the channel reports ``skipped_no_client`` and the ladder proceeds - no
real call is ever made from tests or unconfigured runs.  Parsing normalises a
payload in the exact on-disk corpus shape ``{amc, files: [{file, status,
sheets: [{sheet, scheme, date, status, plans: {plan: {holdings}}}]}]}`` into
the record ``webapp/db.py::_load_advisorkhoj_schemes`` consumes: holdings stay
grouped per scheme under their sheet's ``plans``, every row keeps the loader's
holding keys (``name``/``isin``/``industry``/``rating``/``quantity``/``value``
/``pct_nav``/``yield``/``section``) and gains ``percent_nav`` mirrored from
``pct_nav`` so ``src.agents.tiers`` can judge coverage (the tier layer reads
``percent_nav``, the loader reads ``pct_nav``).  Anything else - messages,
empty file lists, sheets without a scheme name or without holdings - returns
``None`` (holdings are never fabricated).

Write discipline: a parsed record is persisted ONLY when it reaches tier T0
``COMPLETE_100`` (strict full-disclosure rule, Σ >= 95).  An incomplete
Advisorkhoj record must never be written: at merge priority 2 it could
displace the index-resolved fallback for a scheme-month while still being
partial.  Writes are append-only and atomic (temp file + ``os.replace``) and
land FLAT directly under ``<out_dir>/`` as
``<amc_slug>_<YYYY-MM>_<digest>.json`` - the flat top-level glob in
``webapp/db.py::_load_advisorkhoj_schemes`` is the loader contract (a nested
tree would be invisible to the merge); an existing file is returned untouched,
never overwritten.

The record's ``document_class`` is stamped from
``src.document_class.classify`` before the write.  The classify signal is the
record's ``source_file`` - the payload's own document path (the corpus's
``files[].file`` value, the same ``inner or rel`` provenance
``src/agents/integrity.py::_index_advisorkhoj`` classifies this corpus with),
falling back to the channel label ``advisorkhoj:monthly_portfolio_disclosure``
- so a republished factsheet classifies ``factsheet_topn`` and a portfolio
document classifies ``full_portfolio`` per OQ-8.

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
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from src.agents import taxonomy
from src.agents.channels import (
    ChannelResult,
    _name_matches,
    evaluate_candidate_payload,
)
from src.agents.escalation import Ticket
from src.document_class import classify as classify_document

CHANNEL = "advisorkhoj"

SOURCE_HOST = "advisorkhoj.com"
BASE_URL = f"https://www.{SOURCE_HOST}"
AK_PARSED_DIR = Path("data") / "parsed" / "advisorkhoj"
# LAYOUT CONTRACT: the webapp loader ``webapp/db.py::_load_advisorkhoj_schemes``
# reads this directory with a FLAT top-level glob (``ADVISORKHOJ_DIR.glob("*.json")``
# - no rglob, no nesting), so write_parsed persists records DIRECTLY under
# out_dir/ as ``<amc_slug>_<YYYY-MM>_<digest>.json``.  The existing corpus uses
# plain ``<AMC name>.json`` names (e.g. ``360 ONE Mutual Fund.json``); both
# namings are flat and both are read by the same glob - do not nest.

DOWNLOAD_CENTRE_PATH = "/form-download-centre/"
DISCLOSURE_CATEGORY_PATH = "/form-download-centre/Mutual/{slug}/Monthly-Portfolio-Disclosures"

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
        raise RuntimeError(f"source_advisorkhoj references unknown taxonomy code {_code!r}")

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

PORTFOLIO_STRATEGY = "monthly_portfolio_disclosure_documents"
PORTFOLIO_SOURCE_LABEL = "advisorkhoj:monthly_portfolio_disclosure"


# ---------------------------------------------------------------------------
# Fetch targets (derived from archive/docs/ADVISORKHOJ_PLAN.md - nothing invented)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchTarget:
    """One Advisorkhoj access step the channel performs (path joined onto
    ``base_url``; ``{slug}`` is substituted for the per-AMC step)."""

    key: str
    path: str
    role: str
    description: str


FETCH_TARGETS: tuple[FetchTarget, ...] = (
    FetchTarget(
        key="download_centre",
        path=DOWNLOAD_CENTRE_PATH,
        role="resolve_amc_slug",
        description=(
            "form-download-centre page payload; the <select id=\"select_company\"> AMC list "
            "is resolved to the ticket AMC's URL slug with the "
            "archive/docs/ADVISORKHOJ_PLAN.md patterns"
        ),
    ),
    FetchTarget(
        key="monthly_portfolio_disclosures",
        path=DISCLOSURE_CATEGORY_PATH,
        role="disclosure_links",
        description=(
            "per-AMC Monthly-Portfolio-Disclosures category page; blue_text links labelled "
            "'Monthly Portfolio Disclosure - {Month Year}' are matched against the ticket's "
            "month and each linked document is fetched"
        ),
    ),
)


# ---------------------------------------------------------------------------
# Low-level helpers (mirror the archive/docs/ADVISORKHOJ_PLAN.md access patterns)
# ---------------------------------------------------------------------------

_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]

_MONTH_LOOKUP: dict[str, int] = {}
for _i, _name in enumerate(_MONTH_NAMES, start=1):
    _MONTH_LOOKUP[_name.casefold()] = _i
    _MONTH_LOOKUP[_name.casefold()[:3]] = _i

_SELECT_RE = re.compile(r'<select[^>]*\bid="select_company"[^>]*>(.*?)</select>',
                        re.IGNORECASE | re.DOTALL)
_OPTION_TAG_RE = re.compile(r"<option\b([^>]*)>([^<]*)</option>", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>([^<]*)</a>", re.IGNORECASE)
_ATTR_HREF_RE = re.compile(r'\bhref="([^"]*)"', re.IGNORECASE)
_ATTR_CLASS_RE = re.compile(r'\bclass="([^"]*)"', re.IGNORECASE)
_ATTR_VALUE_RE = re.compile(r'\bvalue="([^"]*)"', re.IGNORECASE)
_DISCLOSURE_LABEL_RE = re.compile(r"monthly\s+portfolio\s+disclosure", re.IGNORECASE)
_LABEL_MONTH_RE = re.compile(r"([A-Za-z]{3,9})\.?[\s-]+(\d{4})")
_MONTH_RE = re.compile(r"(\d{4})[-/](\d{1,2})")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_YM_DATE_RE = re.compile(r"\d{4}-\d{2}")
_DDMMM_DATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")


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


def _headers(base_url: str, referer: str = "") -> dict[str, str]:
    headers = dict(UA)
    if referer:
        headers["Referer"] = referer
    return headers


def disclosure_category_url(slug: str, base_url: str = BASE_URL) -> str:
    """The AMC's Monthly-Portfolio-Disclosures category page URL (the documented
    ``/form-download-centre/Mutual/{slug}/...`` pattern)."""
    return base_url + DISCLOSURE_CATEGORY_PATH.format(slug=str(slug or "").strip("/"))


def _ticket_month(month: object) -> tuple[int, int] | None:
    """(year, month) for a ticket month token like ``2026-08``; ``None`` when
    absent or unparseable."""
    match = _MONTH_RE.search(str(month or ""))
    if not match:
        return None
    year, mon = int(match.group(1)), int(match.group(2))
    if not 1 <= mon <= 12:
        return None
    return year, mon


def _label_month(label: str) -> tuple[int, int] | None:
    """(year, month) a disclosure link label carries (``... - August 2026``)."""
    match = _LABEL_MONTH_RE.search(str(label or ""))
    if not match:
        return None
    mon = _MONTH_LOOKUP.get(match.group(1).casefold()[:3])
    if mon is None:
        return None
    return int(match.group(2)), mon


def _iso_date(text: object) -> str:
    """Disclosure as-of date carried by a sheet's ``date`` field; ``""`` when
    none is parseable."""
    value = str(text or "").strip()
    if not value:
        return ""
    match = _ISO_DATE_RE.search(value)
    if match:
        return match.group(0)
    match = _DDMMM_DATE_RE.search(value)
    if match:
        day, mon, year = int(match.group(1)), match.group(2).title(), int(match.group(3))
        if mon in _MON:
            return f"{year}-{_MON.index(mon) + 1:02d}-{day:02d}"
    if _YM_DATE_RE.fullmatch(value):
        return value
    return ""


def _resolve_amc_slug(amc: object, client: object, base_url: str) -> str:
    """Advisorkhoj URL slug for ``amc`` from the download-centre's AMC list
    (``<select id="select_company">``, the documented AMC-list pattern).

    Returns ``""`` when the AMC is not listed (graceful nothing-found);
    transport failures propagate to the caller's taxonomy mapping (a block
    must be reported, never swallowed).
    """
    resp = client.get(
        f"{base_url}{DOWNLOAD_CENTRE_PATH}",
        headers=_headers(base_url),
    )
    text = _resp_text(resp)
    block = _SELECT_RE.search(text)
    scope = block.group(1) if block else text
    for attrs, name in _OPTION_TAG_RE.findall(scope):
        if not _name_matches(name, str(amc or "")):
            continue
        value_m = _ATTR_VALUE_RE.search(attrs)
        slug = value_m.group(1).strip() if value_m else ""
        if not slug or "://" in slug or slug.startswith("/") or " " in slug:
            slug = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-")
        return slug
    return ""


def _blue_text_links(text: str) -> list[tuple[str, str]]:
    """(href, label) pairs of the download-centre's ``blue_text`` document links
    (the documented ``<li><a class="blue_text" ... href=...>LABEL</a></li>``
    markup)."""
    links: list[tuple[str, str]] = []
    for attrs, label in _ANCHOR_RE.findall(text):
        class_m = _ATTR_CLASS_RE.search(attrs)
        if not class_m or "blue_text" not in class_m.group(1).casefold():
            continue
        href_m = _ATTR_HREF_RE.search(attrs)
        if not href_m:
            continue
        href = href_m.group(1).strip()
        if href:
            links.append((href, label.strip()))
    return links


def _disclosure_targets(text: str, month: tuple[int, int] | None) -> list[str]:
    """Href values of the monthly-portfolio-disclosure documents a category
    page lists for ``month`` (blue_text links labelled ``Monthly Portfolio
    Disclosure - {Month Year}``); ``[]`` when no link matches."""
    if not month:
        return []
    targets: list[str] = []
    seen: set[str] = set()
    for href, label in _blue_text_links(text):
        if not _DISCLOSURE_LABEL_RE.search(label):
            continue
        if _label_month(label) != month:
            continue
        if href not in seen:
            seen.add(href)
            targets.append(href)
    return targets


# ---------------------------------------------------------------------------
# Fetch + parse + persist
# ---------------------------------------------------------------------------


def fetch_candidates(
    ticket: Ticket,
    *,
    client: object = None,
    base_url: str = BASE_URL,
) -> list[dict]:
    """Raw Advisorkhoj payloads that may carry the ticket's portfolio rows.

    Consults :data:`FETCH_TARGETS` in order: the download-centre page resolves
    the ticket's AMC to its Advisorkhoj slug (not listed -> ``[]``, graceful
    nothing-found), the Monthly-Portfolio-Disclosures category page yields the
    disclosure links for ``ticket.month``, and each linked document is fetched.
    ``client`` MUST be injected (an object exposing ``.get(url, **kw)``); with
    ``client=None`` this returns ``[]`` without any network call and
    :func:`run` reports ``skipped_no_client``.  A linked document is consumed
    only when its body parses as JSON (the corpus's nested portfolio shape) -
    binary XLSX/PDF/HTML bodies are skipped (parsing those belongs to the
    amc_website pipeline).  Raw payloads are returned as received;
    :func:`parse_candidate` decides recognisability.
    """
    if client is None:
        return []
    slug = _resolve_amc_slug(ticket.amc, client, base_url)
    if not slug:
        return []
    month = _ticket_month(ticket.month)
    if not month:
        return []
    category_url = disclosure_category_url(slug, base_url)
    resp = client.get(category_url, headers=_headers(base_url, category_url))
    hrefs = _disclosure_targets(_resp_text(resp), month)
    if not hrefs:
        return []
    payloads: list[dict] = []
    for href in hrefs:
        url = urljoin(f"{base_url}/", href)
        resp = client.get(url, headers=_headers(base_url, category_url))
        try:
            payload = _resp_json(resp)
        except Exception:
            continue
        if isinstance(payload, (dict, list)):
            payloads.append(payload)
    return payloads


def _normalized_row(row: Mapping) -> dict:
    """Loader-facing holding row with ``percent_nav`` mirrored from ``pct_nav``.

    The webapp loader reads ``pct_nav``; ``src.agents.tiers`` reads
    ``percent_nav`` - the mirrored key lets one row serve both consumers
    without touching the loader's own keys.
    """
    out = dict(row)
    out.setdefault("percent_nav", out.get("pct_nav", ""))
    return out


def parse_candidate(payload: object, *, source_file: str = "") -> dict | None:
    """Convert a raw Advisorkhoj payload into the ``_load_advisorkhoj_schemes``
    record shape.

    Recognised payloads are the corpus's nested portfolio shape ``{amc, files:
    [{file, status, sheets: [{sheet, scheme, date, status, plans: {plan:
    {holdings}}}]}]}``: holdings stay grouped per scheme under their sheet's
    ``plans``, each row keeps the loader's holding keys and gains
    ``percent_nav`` mirrored from ``pct_nav`` (see :func:`_normalized_row`),
    and the record's ``as_of`` is the first parseable sheet date.  The
    record's ``source_file`` is the payload's own document path
    (``files[].file`` - the provenance ``src/agents/integrity.py`` classifies
    this corpus with) falling back to the ``source_file`` argument.  Anything
    else - messages, empty file lists, sheets without a scheme name or without
    holdings - returns ``None`` (holdings are never fabricated).
    """
    if not isinstance(payload, Mapping):
        return None
    files_in = payload.get("files")
    if not isinstance(files_in, list):
        return None
    files_out: list[dict] = []
    as_of = ""
    document_path = ""
    for file_entry in files_in:
        if not isinstance(file_entry, Mapping):
            continue
        sheets_out: list[dict] = []
        for sheet in file_entry.get("sheets") or []:
            if not isinstance(sheet, Mapping):
                continue
            scheme = str(sheet.get("scheme") or "").strip()
            if not scheme:
                continue
            plans_in = sheet.get("plans")
            if not isinstance(plans_in, Mapping):
                continue
            plans_out: dict[str, dict] = {}
            for plan_name, plan in plans_in.items():
                if not isinstance(plan, Mapping):
                    continue
                rows = plan.get("holdings")
                if not isinstance(rows, list):
                    continue
                kept = [_normalized_row(row) for row in rows if isinstance(row, Mapping)]
                if kept:
                    plans_out[str(plan_name)] = {"holdings": kept}
            if not plans_out:
                continue
            date_text = str(sheet.get("date") or "").strip()
            if not as_of:
                as_of = _iso_date(date_text)
            sheets_out.append({
                "sheet": str(sheet.get("sheet") or ""),
                "scheme": scheme,
                "date": date_text,
                "status": str(sheet.get("status") or ""),
                "plans": plans_out,
            })
        if not sheets_out:
            continue
        if not document_path:
            document_path = str(file_entry.get("file") or "").strip()
        files_out.append({
            "file": str(file_entry.get("file") or ""),
            "status": str(file_entry.get("status") or ""),
            "sheets": sheets_out,
        })
    if not files_out:
        return None
    return {
        "amc": str(payload.get("amc") or "").strip(),
        "as_of": as_of,
        "files": files_out,
        "source_file": document_path or str(source_file or ""),
    }


def write_parsed(record: Mapping, *, out_dir: str | Path = AK_PARSED_DIR) -> Path:
    """Atomically persist one parsed record; append-only, never overwrites.

    FLAT layout (loader contract): ``<out_dir>/<amc_slug>_<YYYY-MM>_<digest>.json``
    - the file sits DIRECTLY under ``out_dir`` because
    ``webapp/db.py::_load_advisorkhoj_schemes`` reads only a flat top-level
    ``ADVISORKHOJ_DIR.glob("*.json")``; a nested ``<AMC>/<YYYY>/<MM>/`` tree
    would be invisible to the merge (Advisorkhoj is merge-priority 2).
    ``YYYY-MM`` comes from the record's ``as_of``; when ``as_of`` is absent the
    short content digest stands in for the stamp.  The digest is the sha256 of
    the canonical record JSON, so re-writing the SAME record resolves to the
    existing file and returns it untouched, while a genuinely different find
    lands in its own sibling file: nothing is ever overwritten or silently
    dropped.  The write itself is atomic (temp file + ``os.replace``).
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
    out_dir: str | Path = AK_PARSED_DIR,
    parse: object = None,
    write: object = None,
    now: object = None,
) -> ChannelResult:
    """Work one escalated ticket through the Advisorkhoj source channel (§11.3 #4).

    Injected dependencies (no real network, no filesystem access of its own):

    * ``client`` - an object exposing ``.get(url, **kw)`` (e.g. an httpx
      client).  ``None`` short-circuits the channel with
      ``skipped_no_client`` so the ladder proceeds to the manual channel; no
      real call is ever made without an injected client.
    * ``out_dir`` - destination directory for the parsed record (written FLAT
      directly under it, see :func:`write_parsed`; default
      :data:`AK_PARSED_DIR`).
    * ``parse`` / ``write`` - optional replacements for
      :func:`parse_candidate` / :func:`write_parsed` with the same signatures.
    * ``now`` is accepted for caller uniformity (episode stamping); the
      channel itself is clock-free.

    Success requires a full-disclosure-class record reaching Σ >= 95
    (``tiers.classify`` -> T0 ``COMPLETE_100``); only then is the record
    written (an incomplete priority-2 record could displace a better fallback)
    and its path reported in ``new_document_paths``.  A factsheet/top-N
    document does NOT close the ticket.  Every exception is mapped through
    ``taxonomy.classify_exception``; ``failure_code`` is never ``None`` on a
    failed run and the queue/DB are never mutated.
    """
    if client is None:
        return ChannelResult(
            channel=CHANNEL,
            success=False,
            reason=(
                f"{REASON_SKIPPED_NO_CLIENT}: no HTTP client injected; Advisorkhoj pages "
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
        reason = f"Advisorkhoj fetch failed ({code}); no portfolio payload obtained"
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
                "no portfolio payload obtained from the Advisorkhoj download-centre "
                "(AMC not listed on advisorkhoj.com, no monthly portfolio disclosure "
                "published for the scheme-month, or the linked documents are not JSON "
                "portfolio payloads); nothing fabricated"
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
            note(code, f"Advisorkhoj payload parse failed ({code})")
            continue
        if not isinstance(record, Mapping) or not record:
            note(
                CODE_MISSING,
                "Advisorkhoj payload is not a recognisable portfolio disclosure "
                "(no scheme-attributed holding rows); nothing fabricated",
            )
            continue

        closed = dict(record)
        closed["amc"] = str(closed.get("amc") or ticket.amc or "").strip()
        source_label = str(closed.get("source_file") or PORTFOLIO_SOURCE_LABEL)
        verdict = evaluate_candidate_payload(
            closed, ticket.scheme, source_file=source_label
        )

        if verdict.is_t0:
            closed["document_class"] = classify_document(
                closed, source_file=source_label
            )
            try:
                path = write_fn(closed, out_dir=out_dir)
            except Exception as exc:
                code = taxonomy.classify_exception(exc) or CODE_PARSER_PARTIAL
                note(code, f"complete Advisorkhoj find could not be written ({code})")
                continue
            return ChannelResult(
                channel=CHANNEL,
                success=True,
                reason=(
                    f"closed by Advisorkhoj {closed['document_class']} record "
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
                f"suspect scale in the Advisorkhoj payload: {verdict.tier.reason}",
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
                f"incomplete {verdict.document_class} Advisorkhoj parse "
                f"(Σ={verdict.tier.coverage_pct:.2f}%): {verdict.tier.reason}; "
                f"not written (Advisorkhoj is merge-priority 2 - only a complete "
                f"record is worth writing)",
            )
        else:
            note(
                CODE_MISSING,
                f"scheme not present in the Advisorkhoj portfolio payload "
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
        reason="no Advisorkhoj candidate closed the ticket",
        new_document_paths=[],
        strategy_used=PORTFOLIO_STRATEGY,
        failure_code=CODE_MISSING,
    )


__all__ = [
    "AK_PARSED_DIR",
    "BASE_URL",
    "CHANNEL",
    "CODE_INCOMPLETE_SUM",
    "CODE_MISSING",
    "CODE_PARSER_PARTIAL",
    "CODE_SUSPECT_SCALE",
    "CODE_TOPN_ONLY",
    "DISCLOSURE_CATEGORY_PATH",
    "DOWNLOAD_CENTRE_PATH",
    "FETCH_TARGETS",
    "FetchTarget",
    "PORTFOLIO_SOURCE_LABEL",
    "PORTFOLIO_STRATEGY",
    "REASON_SKIPPED_NO_CLIENT",
    "SOURCE_HOST",
    "UA",
    "WAF_CODE",
    "disclosure_category_url",
    "fetch_candidates",
    "parse_candidate",
    "run",
    "write_parsed",
]
