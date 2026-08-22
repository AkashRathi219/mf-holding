#!/usr/bin/env python3
"""MF Portfolio Holdings Agent - Fetch and parse monthly portfolio holdings from all AMCs."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

import click
import yaml

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

from src.amc_adapters import get_adapter
from src.amc_adapters.base import extract_month_year
from src.amfi_ter import (
    build_report,
    download_ter_excel,
    load_navall_names,
    load_ter_schemes,
    load_universe,
)
from src.nav_daily import update_latest_navs
from src.stock_actions import run as run_stock_actions
from src.stock_identity import build_identity
from src.stock_price import run as run_stock_price
from src.stock_refresh import refresh_all
from src.stock_reports import run as run_stock_reports
from src.stock_status import report as stock_status_report
from src.pdf_downloader import DocumentDownloader
from src.pdf_parser import parse_pdf, save_parsed_data
from src.excel_parser import parse_excel, save_excel_parsed_data
from src.html_parser import parse_html, save_html_parsed_data
from src.zip_parser import parse_zip, save_zip_parsed_data
from src.scheduler import MonthlyScheduler
from src.utils import setup_logging

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent


def load_config() -> dict:
    config_path = BASE_DIR / "config" / "settings.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_amc_registry() -> list[dict]:
    registry_path = BASE_DIR / "config" / "amc_registry.json"
    with open(registry_path, encoding="utf-8-sig") as f:
        return json.load(f)


def _amc_folder_name(amc_name: str) -> str:
    return amc_name.replace(" ", "_").replace("/", "-")


def _detect_month_year(text: str) -> tuple[int, int] | None:
    """Best-effort month/year extraction from a manual filename."""
    return extract_month_year(text)


def _match_amc(text: str, registry: list[dict]) -> dict | None:
    """Match an AMC from a folder name or filename against the registry."""
    tl = text.lower().strip()
    # Exact match first
    for amc in registry:
        if amc["mf_name"].lower() == tl:
            return amc
    # Substring match (folder = "Kotak Mahindra Mutual Fund")
    for amc in registry:
        if amc["mf_name"].lower() in tl:
            return amc
    # Reverse: search registry name within the text (root-level files)
    for amc in registry:
        key = amc["mf_name"].lower()
        if key in tl:
            return amc
    return None


def parse_document(doc_path: Path) -> dict:
    """Parse a document based on its file type."""
    suffix = doc_path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(doc_path)
    elif suffix in (".xlsx", ".xls"):
        return parse_excel(doc_path)
    elif suffix == ".zip":
        return parse_zip(doc_path)
    elif suffix == ".csv":
        import pandas as pd
        df = pd.read_csv(doc_path)
        return {
            "source_file": str(doc_path),
            "file_type": "csv",
            "metadata": {"columns": list(df.columns), "rows": len(df)},
            "data": df.to_dict(orient="records"),
        }
    elif suffix == ".html":
        return parse_html(doc_path)
    else:
        return parse_pdf(doc_path)


def save_parsed(doc_path: Path, parsed: dict, amc_name: str, year: int, month: int, parsed_dir: Path):
    """Save parsed data to the appropriate output directory."""
    safe_amc = amc_name.replace(" ", "_").replace("/", "-")
    out_dir = parsed_dir / safe_amc / str(year) / f"{month:02d}"
    stem = doc_path.stem

    suffix = doc_path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        save_excel_parsed_data(parsed, out_dir, stem)
    elif suffix == ".zip":
        save_zip_parsed_data(parsed, out_dir, stem)
    elif suffix == ".csv":
        _save_csv_data(parsed, out_dir, stem)
    elif suffix == ".html":
        save_html_parsed_data(parsed, out_dir, stem)
    else:
        save_parsed_data(parsed, out_dir, stem)


def _save_csv_data(parsed: dict, out_dir: Path, stem: str) -> None:
    """Persist raw CSV rows as JSON + a normalized CSV."""
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"Saved JSON: {json_path}")

    records = parsed.get("data", [])
    if records:
        df = pd.DataFrame(records)
        csv_path = out_dir / f"{stem}.csv"
        df.to_csv(csv_path, index=False)
        logger.info(f"Saved CSV: {csv_path} ({len(records)} rows)")


# Filenames that are clearly NOT portfolio holdings/factsheet documents.
_IRRELEVANT_PATTERNS = (
    "tracking-error", "tracking error",
    "market-flash", "market flash",
    "dividend-declaration", "dividend declaration",
    "addendum", "notice",
    "riskometer", "risk-ometer",
    "nfo", "new fund offer",
    "press-release", "press release",
    "annual-report", "annual report",
    "statement of additional", "sai",
    "scheme information",
    "investor-charter", "investor charter",
    "kyc-form", "kyc", "fatca", "crs", "ubo",
    "stp", "sip", "swp", "redemption", "transmission",
    "tax-reckoner", "tax reckoner",
    "application-form", "application form",
    "empanelement", "empanelement", "grievance",
    "policy", "circular", "commission", "soft-dollar",
    "stewardship", "proxy-voting", "proxy voting",
    "valuation-policy", "voting", "custodian",
    "definitions", "glossary", "mis-selling",
    "unclaimed", "lock-in", "non-business", "nri-corner",
    "iap", "investor-awareness", "investor awareness",
    "annual-aum", "aaum", "aum-", "aum ",
    "bank-list", "live-bank",
)


def _is_relevant_document(filename: str) -> bool:
    """Keep only portfolio/factsheet-style documents, drop obvious non-holdings files."""
    name = (filename or "").lower()
    # Explicitly keep monthly/fortnightly portfolio and scheme summary docs.
    if "portfolio" in name or "factsheet" in name or "scheme summary" in name:
        return True
    # ZIP archives are per-scheme monthly-portfolio bundles - always keep.
    if name.endswith(".zip"):
        return True
    return not any(pattern in name for pattern in _IRRELEVANT_PATTERNS)


def _extract_scheme_names(parsed: dict) -> list[str]:
    """Collect scheme names from a parsed document."""
    names = []
    schemes = parsed.get("schemes", {})
    for code, scheme in schemes.items():
        fund_name = scheme.get("fund_name", "") if isinstance(scheme, dict) else ""
        if fund_name:
            names.append(fund_name)
        elif isinstance(scheme, dict) and scheme.get("scheme_name"):
            names.append(f"{code} ({scheme['scheme_name']})")
        else:
            names.append(str(code))
    return names


def _scan_parsed_for_report(parsed_dir: Path, year: int, month: int) -> dict:
    """Rebuild report entries for AMCs that already have parsed output on disk."""
    entries: dict[str, dict] = {}
    month_dir = f"{month:02d}"
    if not parsed_dir.exists():
        return entries

    for amc_dir in parsed_dir.iterdir():
        if not amc_dir.is_dir():
            continue
        amc_month_dir = amc_dir / str(year) / month_dir
        if not amc_month_dir.is_dir():
            continue

        entry = {
            "amc": amc_dir.name,
            "target_month": month,
            "target_year": year,
            "as_of_month": month,
            "as_of_year": year,
            "fallback": False,
            "status": "success",
            "documents": [],
            "schemes": [],
        }

        for json_path in sorted(amc_month_dir.glob("*.json")):
            if json_path.name.startswith("report_"):
                continue
            try:
                with open(json_path, encoding="utf-8") as f:
                    parsed = json.load(f)
            except Exception:
                continue

            source = parsed.get("source_file", json_path.name)
            entry["documents"].append(Path(source).name)
            entry["schemes"].extend(_extract_scheme_names(parsed))

        entry["schemes"] = sorted(set(entry["schemes"]))
        entries[amc_dir.name] = entry

    return entries


def _normalize_amc(name: str) -> str:
    """Canonical key for an AMC name (matches the on-disk folder naming)."""
    return name.replace(" ", "_").replace("/", "-").lower()


def _save_report(report: list[dict], parsed_dir: Path, year: int, month: int) -> None:
    """Persist the per-AMC documents + scheme names report to a JSON file.

    Merges entries from the current run with whatever is already parsed on
    disk (so resumes don't lose earlier AMCs), de-duplicating AMCs whose
    display name and on-disk folder name differ only by separators/case.
    """
    merged: dict[str, dict] = {}

    def _absorb(entry: dict) -> None:
        key = _normalize_amc(entry["amc"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = entry
            return
        existing["documents"] = sorted(set(existing.get("documents", []) + entry.get("documents", [])))
        existing["schemes"] = sorted(set(existing.get("schemes", []) + entry.get("schemes", [])))
        if entry.get("as_of_month") is not None:
            existing["as_of_month"] = entry["as_of_month"]
            existing["as_of_year"] = entry["as_of_year"]
            existing["fallback"] = entry.get("fallback", existing.get("fallback", False))
            existing["status"] = entry.get("status", existing.get("status", "success"))

    for entry in report:
        _absorb(entry)
    for entry in _scan_parsed_for_report(parsed_dir, year, month).values():
        _absorb(entry)

    final_report = sorted(merged.values(), key=lambda e: e["amc"].lower())
    parsed_dir.mkdir(parents=True, exist_ok=True)
    report_path = parsed_dir / f"report_{year}_{month:02d}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"Saved report: {report_path} ({len(final_report)} AMCs)")


async def run_pipeline(
    year: int,
    month: int,
    amc_filter: str | None = None,
    start_from: str | None = None,
):
    """Run the full fetch + parse pipeline."""
    config = load_config()
    amcs = load_amc_registry()

    if amc_filter:
        amcs = [a for a in amcs if amc_filter.lower() in a["mf_name"].lower()]
        if not amcs:
            logger.error(f"No AMC found matching '{amc_filter}'")
            return

    if start_from:
        start_idx = next(
            (i for i, a in enumerate(amcs) if start_from.lower() in a["mf_name"].lower()),
            None,
        )
        if start_idx is None:
            logger.error(f"No AMC found matching start-from '{start_from}'")
            return
        amcs = amcs[start_idx:]
        logger.info(f"Resuming from: {amcs[0]['mf_name']} ({len(amcs)} AMCs remaining)")

    docs_dir = BASE_DIR / config["paths"]["pdfs_dir"]
    parsed_dir = BASE_DIR / config["paths"]["parsed_dir"]
    fetch_cfg = config.get("fetch", {})
    downloader = DocumentDownloader(
        output_dir=docs_dir,
        delay_between_requests=fetch_cfg.get("delay_between_requests", 2.0),
        timeout=fetch_cfg.get("timeout", 120.0),
        verify_ssl=fetch_cfg.get("verify_ssl", False),
    )

    results = {"success": [], "failed": [], "skipped": []}
    report = []

    for amc in amcs:
        amc_name = amc["mf_name"]
        logger.info(f"{'='*60}")
        logger.info(f"Processing: {amc_name}")
        logger.info(f"{'='*60}")

        portfolio_url = amc.get("amc_monthly_portfolio_disclosure", "")
        factsheet_url = amc.get("amc_monthly_mf_factsheets", "")

        if not portfolio_url and not factsheet_url:
            logger.warning(f"  No URLs available for {amc_name}, skipping")
            results["skipped"].append(amc_name)
            continue

        report_entry = {
            "amc": amc_name,
            "target_month": month,
            "target_year": year,
            "as_of_month": None,
            "as_of_year": None,
            "fallback": False,
            "status": "skipped",
            "documents": [],
            "schemes": [],
        }

        try:
            adapter = get_adapter(amc_name)
            doc_links = await adapter.discover_documents(
                portfolio_url=portfolio_url,
                factsheet_url=factsheet_url,
                target_month=month,
                target_year=year,
            )

            actual_month, actual_year = month, year
            used_fallback = False

            if not doc_links:
                # Fall back to the most recent document dated on/before the target month.
                all_links = await adapter.discover_documents_all(
                    portfolio_url=portfolio_url,
                    factsheet_url=factsheet_url,
                )
                dated = [
                    link for link in all_links
                    if link.month is not None and link.year is not None
                    and (link.year, link.month) <= (year, month)
                ]
                if dated:
                    latest = max((link.year, link.month) for link in dated)
                    doc_links = [link for link in dated if (link.year, link.month) == latest]
                    actual_year, actual_month = latest
                    used_fallback = True
                    logger.info(
                        f"  No exact match for {year}-{month:02d}; falling back to "
                        f"{actual_year}-{actual_month:02d} ({len(doc_links)} doc(s))"
                    )

            if not doc_links:
                logger.warning(f"  No documents found for {amc_name} ({year}-{month:02d})")
                results["skipped"].append(amc_name)
                report.append(report_entry)
                continue

            doc_links = [link for link in doc_links if _is_relevant_document(link.filename)]
            if not doc_links:
                logger.warning(
                    f"  Only irrelevant documents found for {amc_name} "
                    f"({year}-{month:02d}), skipping"
                )
                results["skipped"].append(amc_name)
                report.append(report_entry)
                continue

            logger.info(f"  Found {len(doc_links)} document(s)")

            downloaded = await downloader.download_all(
                document_links=doc_links,
                amc_name=amc_name,
                year=actual_year,
                month=actual_month,
            )

            for doc_path in downloaded:
                logger.info(f"  Parsing: {doc_path.name}")
                parsed = parse_document(doc_path)
                parsed["amc_name"] = amc_name
                parsed["fetch_month"] = actual_month
                parsed["fetch_year"] = actual_year
                save_parsed(doc_path, parsed, amc_name, actual_year, actual_month, parsed_dir)
                report_entry["documents"].append(doc_path.name)
                report_entry["schemes"].extend(_extract_scheme_names(parsed))

            report_entry["as_of_month"] = actual_month
            report_entry["as_of_year"] = actual_year
            report_entry["fallback"] = used_fallback
            report_entry["status"] = "success"
            report_entry["schemes"] = sorted(set(report_entry["schemes"]))
            results["success"].append(amc_name)

        except Exception as e:
            logger.error(f"  Error processing {amc_name}: {e}")
            report_entry["status"] = "failed"
            results["failed"].append(amc_name)

        report.append(report_entry)

    _save_report(report, parsed_dir, year, month)

    try:
        from src.pdf_agents import _shutdown_pool
        _shutdown_pool()
    except Exception:
        pass

    logger.info(f"\n{'='*60}")
    logger.info("PIPELINE SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"  Success: {len(results['success'])}")
    logger.info(f"  Failed:  {len(results['failed'])}")
    logger.info(f"  Skipped: {len(results['skipped'])}")
    if results["failed"]:
        logger.info(f"  Failed AMCs: {', '.join(results['failed'])}")
    if results["skipped"]:
        logger.info(f"  Skipped AMCs: {', '.join(results['skipped'])}")

    return results


def _setup_from_config():
    """Apply logging settings from config/settings.yaml."""
    config = load_config()
    log_cfg = config.get("logging", {})
    paths_cfg = config.get("paths", {})
    setup_logging(
        log_dir=BASE_DIR / paths_cfg.get("logs_dir", "logs"),
        level=log_cfg.get("level", "INFO"),
    )


def rebuild_report(year: int, month: int) -> None:
    """Rebuild the consolidated per-AMC report from the filesystem (parsed_data)."""
    from collections import Counter

    config = load_config()
    parsed_dir = BASE_DIR / config["paths"]["parsed_dir"]
    registry = load_amc_registry()

    report = []
    for amc in registry:
        amc_name = amc["mf_name"]
        amc_dir = parsed_dir / _amc_folder_name(amc_name)
        best = None
        docs, schemes = [], []
        if amc_dir.is_dir():
            for year_dir in amc_dir.iterdir():
                if not year_dir.is_dir() or not year_dir.name.isdigit():
                    continue
                y = int(year_dir.name)
                for month_dir in year_dir.iterdir():
                    if not month_dir.is_dir() or not month_dir.name.isdigit():
                        continue
                    m = int(month_dir.name)
                    if (y, m) > (year, month):
                        continue
                    has = [p for p in month_dir.glob("*.json") if not p.name.startswith("report_")]
                    if has and (best is None or (y, m) > best):
                        best = (y, m)
                        docs, schemes = [], []
                        for jp in sorted(month_dir.glob("*.json")):
                            if jp.name.startswith("report_"):
                                continue
                            try:
                                parsed = json.load(open(jp, encoding="utf-8"))
                            except Exception:
                                continue
                            src = Path(parsed.get("source_file", jp.name)).name
                            docs.append(src)
                            schemes.extend(_extract_scheme_names(parsed))

        if best is None:
            report.append({
                "amc": amc_name, "target_month": month, "target_year": year,
                "as_of_month": None, "as_of_year": None, "fallback": False,
                "status": "no_documents", "documents": [], "schemes": [],
            })
        else:
            y, m = best
            report.append({
                "amc": amc_name, "target_month": month, "target_year": year,
                "as_of_month": m, "as_of_year": y,
                "fallback": (y, m) != (year, month),
                "status": "success",
                "documents": sorted(set(docs)),
                "schemes": sorted(set(schemes)),
            })

    _save_report(report, parsed_dir, year, month)
    counts = Counter(e["status"] for e in report)
    logger.info(f"Report rebuilt: {dict(counts)}")


def _parse_single_doc(doc: Path, amc_name: str, year: int, month: int, parsed_dir: Path) -> bool:
    """Parse a single document into parsed_data/. Returns True on success."""
    parsed_json = parsed_dir / _amc_folder_name(amc_name) / str(year) / f"{month:02d}" / f"{doc.stem}.json"
    if parsed_json.exists():
        return False
    try:
        parsed = parse_document(doc)
        parsed["amc_name"] = amc_name
        parsed["fetch_month"] = month
        parsed["fetch_year"] = year
        save_parsed(doc, parsed, amc_name, year, month, parsed_dir)
        return True
    except Exception as e:
        logger.error(f"  parse failed {doc.name}: {e}")
        return False


def run_ingest(
    amc_filter: str | None = None,
    default_year: int | None = None,
    default_month: int | None = None,
) -> None:
    """Ingest manually downloaded documents.

    Two sources are handled:
      1. files placed under manual_downloads/{AMC Name}/ (and bare files whose
         filename names the AMC) -> auto-moved to pdfs/ and parsed.
      2. any document already under pdfs/ that has no parsed output -> parsed.
    The consolidated report is rebuilt afterwards.
    """
    config = load_config()
    registry = load_amc_registry()
    pdfs_dir = BASE_DIR / config["paths"]["pdfs_dir"]
    parsed_dir = BASE_DIR / config["paths"]["parsed_dir"]
    manual_dir = BASE_DIR / "data" / "raw" / "manual_ingest"
    manual_dir.mkdir(parents=True, exist_ok=True)

    moved = parsed = skipped = 0
    now = datetime.now()
    default_year = default_year or now.year
    default_month = default_month or now.month

    def _matches_filter(amc_name: str) -> bool:
        return amc_filter is None or amc_filter.lower() in amc_name.lower()

    def _resolve_ym(fname: str) -> tuple[int, int] | None:
        # Both branches return (month, year) - extract_month_year yields
        # (month, year), and the default mirrors that order.
        ym = _detect_month_year(fname)
        if ym is not None:
            return ym
        # No month/year in the filename -> fall back to the CLI-provided default.
        logger.warning(f"  No month/year in '{fname}'; defaulting to {default_year}-{default_month:02d}")
        return (default_month, default_year)

    # ---- 1. manual_downloads subfolders: {AMC Name}/files ----
    for amc_dir in sorted(manual_dir.iterdir()):
        if amc_dir.is_file():
            continue  # bare file handled below
        amc = _match_amc(amc_dir.name, registry)
        if not amc:
            logger.warning(f"No registry AMC matches folder '{amc_dir.name}', skipping")
            continue
        if not _matches_filter(amc["mf_name"]):
            continue
        for f in sorted(amc_dir.iterdir()):
            if not f.is_file():
                continue
            ym = _resolve_ym(f.name)
            if ym is None:
                skipped += 1
                continue
            month, year = ym
            dest_dir = pdfs_dir / _amc_folder_name(amc["mf_name"]) / str(year) / f"{month:02d}"
            dest_dir.mkdir(parents=True, exist_ok=True)
            new_path = dest_dir / f.name
            if new_path.exists():
                logger.warning(f"  Already exists: {new_path}, skipping")
                skipped += 1
                continue
            shutil.move(str(f), str(new_path))
            moved += 1
            if _parse_single_doc(new_path, amc["mf_name"], year, month, parsed_dir):
                parsed += 1
            logger.info(f"  Ingested {amc['mf_name']} {year}-{month:02d}: {f.name}")
        try:
            amc_dir.rmdir()
        except OSError:
            pass

    # ---- 1b. bare files at manual_downloads/ root (AMC name in filename) ----
    for f in sorted(manual_dir.iterdir()):
        if not f.is_file():
            continue
        amc = _match_amc(f.name, registry)
        if not amc:
            logger.warning(f"Cannot match AMC for '{f.name}', skipping (use a subfolder named after the AMC)")
            skipped += 1
            continue
        if not _matches_filter(amc["mf_name"]):
            continue
        ym = _resolve_ym(f.name)
        if ym is None:
            skipped += 1
            continue
        month, year = ym
        dest_dir = pdfs_dir / _amc_folder_name(amc["mf_name"]) / str(year) / f"{month:02d}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        new_path = dest_dir / f.name
        if new_path.exists():
            logger.warning(f"  Already exists: {new_path}, skipping")
            skipped += 1
            continue
        shutil.move(str(f), str(new_path))
        moved += 1
        if _parse_single_doc(new_path, amc["mf_name"], year, month, parsed_dir):
            parsed += 1
        logger.info(f"  Ingested {amc['mf_name']} {year}-{month:02d}: {f.name}")

    # ---- 2. backfill: parse any pdfs doc without parsed output ----
    name_map = {_amc_folder_name(a["mf_name"]): a["mf_name"] for a in registry}
    for amc_dir in sorted(pdfs_dir.iterdir()):
        if not amc_dir.is_dir():
            continue
        amc_name = name_map.get(amc_dir.name, amc_dir.name)
        if not _matches_filter(amc_name):
            continue
        for year_dir in amc_dir.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            year = int(year_dir.name)
            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir() or not month_dir.name.isdigit():
                    continue
                month = int(month_dir.name)
                for doc in sorted(month_dir.iterdir()):
                    if not doc.is_file():
                        continue
                    if _parse_single_doc(doc, amc_name, year, month, parsed_dir):
                        parsed += 1

    logger.info(f"Ingest complete: moved={moved} parsed={parsed} skipped={skipped}")
    rebuild_report(now.year, now.month)


@click.group()
def cli():
    """MF Portfolio Holdings Agent"""
    pass


@cli.command()
@click.option("--year", "-y", type=int, default=datetime.now().year)
@click.option("--month", "-m", type=int, default=datetime.now().month)
@click.option("--amc", "-a", type=str, default=None, help="Filter by AMC name (partial match)")
@click.option("--start", "-s", "start_from", type=str, default=None,
              help="Resume from the AMC matching this name (inclusive)")
def run(year, month, amc, start_from):
    """Fetch and parse portfolio holdings for a given month."""
    _setup_from_config()
    asyncio.run(run_pipeline(year=year, month=month, amc_filter=amc, start_from=start_from))


@cli.command()
def list_amcs():
    """List all AMCs in the registry."""
    _setup_from_config()
    amcs = load_amc_registry()
    click.echo(f"\n{'No.':<5} {'AMC Name':<45} {'Portfolio URL':<50}")
    click.echo("-" * 100)
    for i, amc in enumerate(amcs, 1):
        url = amc.get("amc_monthly_portfolio_disclosure", "N/A")
        if url:
            url = url[:47] + "..." if len(url) > 50 else url
        click.echo(f"{i:<5} {amc['mf_name']:<45} {url}")


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
def parse(file_path):
    """Parse a single PDF or Excel file."""
    _setup_from_config()
    path = Path(file_path)
    result = parse_document(path)
    click.echo(json.dumps(result, indent=2, default=str, ensure_ascii=False))


@cli.command()
@click.option("--amc", "-a", type=str, default=None,
              help="Only ingest files for an AMC matching this name (partial match)")
@click.option("--year", "-y", type=int, default=None,
              help="Month to assign when not detectable from the filename")
@click.option("--month", "-m", type=int, default=None,
              help="Year to assign when not detectable from the filename")
def ingest(amc, year, month):
    """Ingest manually downloaded documents.

    Drop files into manual_downloads/{AMC Name}/ (folder named after the AMC,
    or a bare file whose filename mentions the AMC). Files whose name carries a
    month/year are auto-dated; others use --year/--month (defaults to now).
    They are moved to pdfs/, parsed, and the report is rebuilt. Also backfills
    any document already under pdfs/ that has no parsed output.
    """
    _setup_from_config()
    run_ingest(amc_filter=amc, default_year=year, default_month=month)


@cli.command()
@click.option("--year", "-y", type=int, default=datetime.now().year)
@click.option("--month", "-m", type=int, default=datetime.now().month)
def report(year, month):
    """Rebuild the consolidated report JSON from parsed data on disk."""
    _setup_from_config()
    rebuild_report(year, month)
    click.echo(f"Report rebuilt for {year}-{month:02d}.")


@cli.command()
@click.option("--month", "-m", type=str, default="07-2026",
              help="AMFI month in MM-YYYY format (default: July 2026)")
@click.option("--year", "-y", type=str, default="2026-2027",
              help="AMFI financial year used for the month lookup")
@click.option("--out", "out_dir", type=click.Path(path_type=Path), default=None,
              help="Output directory (default: data/reference)")
def ter(month, year, out_dir):
    """Download AMFI TER for a month and report universe schemes missing TER.

    Downloads the AMFI 'TER of MF Schemes' Excel export (the export covers all
    fund types - open ended, interval, close ended - and all categories in one
    file), consolidates it to scheme level, maps the universe (Combined NAV
    file) onto it by fund name, and reports which schemes have no TER.
    """
    _setup_from_config()
    config = load_config()
    base = BASE_DIR
    out_dir = out_dir or (base / config["paths"]["reference_dir"])
    raw_dir = base / "data" / "raw" / "ter"

    universe_path = base / "data" / "universe" / "Combined NAV - 14-Aug-2026.csv"
    navall_path = base / "data" / "universe" / "navall.txt"

    xlsx = download_ter_excel(month, year, raw_dir)
    ter_df = load_ter_schemes(xlsx)
    universe = load_universe(universe_path)
    navall = load_navall_names(navall_path)

    report = build_report(universe, ter_df, navall)
    mapped = report["mapped"]

    out_dir.mkdir(parents=True, exist_ok=True)
    scheme_csv = out_dir / f"ter_{month}_schemes.csv"
    ter_df.drop(columns=["_key"]).to_csv(scheme_csv, index=False)
    mapped_csv = out_dir / f"ter_{month}_universe.csv"
    mapped.to_csv(mapped_csv, index=False)
    missing_csv = out_dir / f"ter_{month}_missing.csv"
    mapped[~mapped["ter_matched"]].to_csv(missing_csv, index=False)

    click.echo("")
    click.echo("=" * 70)
    click.echo(f"AMFI TER coverage for {month} (all fund types & categories)")
    click.echo("=" * 70)
    click.echo(f"  Universe rows (fund+plan) : {report['total_rows']}")
    click.echo(f"  Matched TER               : {report['matched_rows']}")
    click.echo(f"  MISSING TER (rows)        : {report['missing_rows']}")
    click.echo(f"  Unique funds in universe  : {report['total_funds']}")
    click.echo(f"  Funds with TER            : {report['matched_funds']}")
    click.echo(f"  MISSING TER (funds)       : {report['missing_funds']}")
    click.echo("  Missing by reason:")
    for reason, n in sorted(report["missing_reasons"].items(), key=lambda x: -x[1]):
        click.echo(f"      {n:>4}  {reason}")
    click.echo(f"  Files: {scheme_csv}")
    click.echo(f"         {mapped_csv}")
    click.echo(f"         {missing_csv}")


@cli.command()
@click.option("--days", "-d", type=int, default=10,
              help="How many past days of NAVs to fetch (default: 10)")
def nav_daily(days):
    """Fetch the latest NAVs for all schemes and append to NAV history."""
    _setup_from_config()
    summary = update_latest_navs(days=days)
    click.echo(json.dumps(summary, indent=2))


@cli.command()
@click.option("--max-age", type=int, default=10,
              help="Flag a scheme/stock stale if its latest value is older than N days")
@click.option("--backfill", is_flag=True,
              help="Re-pull stale NAV histories (last known date -> today)")
def nav_freshness(max_age, backfill):
    """Check NAV/price freshness; optionally backfill stale fund histories."""
    _setup_from_config()
    from src.nav_freshness import run_freshness
    report = run_freshness(max_age_days=max_age, backfill=backfill)
    click.echo(json.dumps(report, indent=2))


@cli.command()
@click.option("--cas", is_flag=True,
              help="Backfill the funds in the CAS sample portfolio")
@click.option("--all", "all_", is_flag=True,
              help="Backfill every scheme present in nav_history")
@click.option("--codes", default="", help="Comma-separated AMFI scheme codes")
@click.option("--days", "-d", type=int, default=20,
              help="How many recent days of AMFI NAVs to fetch")
def nav_backfill(cas, all_, codes, days):
    """Refresh NAVs from AMFI for the given funds (CAS sample, all schemes, or codes)."""
    _setup_from_config()
    from src.nav_freshness import all_codes, backfill_codes_amfi, cas_sample_codes
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list and cas:
        code_list = cas_sample_codes()
    if not code_list and all_:
        code_list = all_codes()
    if not code_list:
        click.echo("No codes given. Use --cas, --all or --codes.")
        raise SystemExit(1)
    click.echo(f"Backfilling {len(code_list)} codes from AMFI (last {days} days)...")
    report = backfill_codes_amfi(code_list, days=days)
    click.echo(json.dumps(report, indent=2))


@cli.command()
@click.option("--out", "-o", default="", help="Write the status JSON to this path")
def nav_status(out):
    """Report which schemes have complete NAV history from inception to the latest."""
    _setup_from_config()
    from src.nav_freshness import completeness_report
    report = completeness_report()
    if out:
        from pathlib import Path
        Path(out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    click.echo(f"Total schemes: {report['total']}  |  Complete: {report['complete']}  |  "
               f"Incomplete: {report['incomplete']}")
    click.echo(json.dumps({
        "total": report["total"], "complete": report["complete"],
        "incomplete": report["incomplete"], "latest_expected": report["latest_expected"]},
        indent=2))


@cli.command()
def stock_identity():
    """Build the ISIN -> NSE symbol map for equity stocks."""
    _setup_from_config()
    ident = build_identity(force=True)
    with_sym = sum(1 for v in ident.values() if v.get("symbol"))
    click.echo(f"Stock identity: {len(ident)} confirmed-equity ISINs, "
               f"{with_sym} with an NSE symbol -> data/stocks/identity.json")


@cli.command()
@click.option("--symbols", "-s", default="", help="Comma-separated NSE symbols to update")
@click.option("--daily", is_flag=True, help="Incremental (recent) update only")
@click.option("--limit", type=int, default=None, help="Max stocks to process")
def stock_price(symbols, daily, limit):
    """Fetch daily closing prices for equity stocks (NSE/Yahoo/manual)."""
    _setup_from_config()
    from src.stock_identity import load_identity
    ident = load_identity()
    syms = [s.strip() for s in symbols.split(",") if s.strip()] or None
    results = run_stock_price(ident=ident, symbols=syms, daily=daily, limit=limit)
    ok = sum(1 for r in results if r.get("status") == "ok")
    click.echo(json.dumps({"total": len(results), "ok": ok}))


@cli.command()
@click.option("--symbols", "-s", default="", help="Comma-separated NSE symbols to update")
@click.option("--limit", type=int, default=None)
def stock_actions(symbols, limit):
    """Fetch dividends & splits for equity stocks."""
    _setup_from_config()
    from src.stock_identity import load_identity
    ident = load_identity()
    syms = [s.strip() for s in symbols.split(",") if s.strip()] or None
    results = run_stock_actions(ident=ident, symbols=syms, limit=limit)
    click.echo(json.dumps({"total": len(results)}))


@cli.command()
@click.option("--symbols", "-s", default="", help="Comma-separated NSE symbols to update")
@click.option("--limit", type=int, default=None)
def stock_reports(symbols, limit):
    """Fetch recent NSE financial-report announcements for equity stocks."""
    _setup_from_config()
    from src.stock_identity import load_identity
    ident = load_identity()
    syms = [s.strip() for s in symbols.split(",") if s.strip()] or None
    results = run_stock_reports(ident=ident, symbols=syms, limit=limit)
    click.echo(json.dumps({"total": len(results)}))


@cli.command()
@click.option("--limit", type=int, default=None)
@click.option("--full", is_flag=True, help="Full backfill (not incremental)")
def stock_refresh(limit, full):
    """Run identity + price + actions + reports for all equity stocks."""
    _setup_from_config()
    summary = refresh_all(daily=not full, limit=limit)
    click.echo(json.dumps(summary, indent=2))


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output JSON only")
def stock_status(as_json):
    """Show stock backfill completion status (price / actions / reports)."""
    _setup_from_config()
    r = stock_status_report()
    if as_json:
        click.echo(json.dumps(r, indent=2))
        return
    click.echo(f"Stock backfill status:")
    click.echo(f"  Universe: {r['total_stocks']} confirmed-equity stocks "
               f"({r['with_symbol']} with NSE symbols)")
    click.echo(f"  Price history: {r['price_done']}/{r['total_stocks']} "
               f"({r['price_pct']}%) — {r['price_points']} points, latest {r['price_latest_date'] or '—'}")
    click.echo(f"  Corporate actions: {r['actions_done']}/{r['total_stocks']} ({r['actions_pct']}%)")
    click.echo(f"  Reports: {r['reports_done']}/{r['total_stocks']} ({r['reports_pct']}%)")


@cli.command()
@click.option("--days", "-d", type=int, default=1,
              help="fetch the last N trading days of NSE bond bulk reports (default 1)")
@click.option("--build-only", is_flag=True,
              help="rebuild the bonds catalog from already-cached raw files")
@click.option("--live", is_flag=True,
              help="also attempt the NSE live debt-market JSON API (Akamai-protected, best-effort)")
def bond_refresh(days, build_only, live):
    """Fetch NSE bond/debt-market reports and rebuild the bond catalog.

    Primary: NSE daily bulk files (corporate-bond master + WDM securities list +
    CBM daily trades) from nsearchives.nseindia.com. Secondary: the live
    'live-analysis-debt-market' JSON API (cookie handshake, best-effort).
    Output: data/reference/bonds_catalog.json (with computed YTM where the
    reported yield is missing)."""
    _setup_from_config()
    from src.bonds import build_catalog, fetch_day, fetch_live_debt_market
    if not build_only:
        got, tries, d = 0, 0, datetime.now().date()
        from datetime import timedelta
        while got < days and tries < days * 3 + 5:
            files = fetch_day(d)
            if files:
                click.echo(f"  {d}: {len(files)} file(s)")
                got += 1
            d -= timedelta(days=1)
            tries += 1
    if live:
        recs = fetch_live_debt_market()
        click.echo(f"  live API: {len(recs)} record(s)")
    catalog = build_catalog()
    click.echo(json.dumps({
        "catalog": str(catalog_path()),
        "as_of": catalog["as_of"], "bonds": catalog["n_bonds"],
        "segments": catalog["segments"], "sources": catalog["sources"]},
        indent=2))


def catalog_path():
    from src.bonds import CATALOG_JSON
    return CATALOG_JSON


def bond_refresh_daily() -> dict:
    """Daily job: pull the latest NSE debt bulk reports and rebuild the
    catalog (used by the scheduler; never raises)."""
    from src.bonds import build_catalog, fetch_day
    from datetime import timedelta
    d = datetime.now().date()
    files = fetch_day(d)
    catalog = build_catalog()
    return {"fetched": {str(d): len(files)} if files else {},
            "as_of": catalog["as_of"], "bonds": catalog["n_bonds"]}


@cli.command()
def schedule_start():
    """Start the scheduler (monthly holdings + daily NAV & stock refresh)."""
    _setup_from_config()

    async def _start():
        config = load_config()
        scheduler = MonthlyScheduler(run_pipeline, config, base_dir=BASE_DIR,
                                     nav_refresh_fn=update_latest_navs,
                                     stock_refresh_fn=refresh_all,
                                     bond_refresh_fn=bond_refresh_daily)
        scheduler.start()

        next_run = scheduler.get_next_run()
        if next_run:
            click.echo(f"Scheduler started. Next run: {next_run}")
        else:
            click.echo("Scheduler started.")

        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            scheduler.stop()

    asyncio.run(_start())


if __name__ == "__main__":
    cli()
