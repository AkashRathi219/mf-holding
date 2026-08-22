from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import pdfplumber

from src.pdf_agents import PdfCoordinatorAgent

logger = logging.getLogger(__name__)


def parse_pdf(pdf_path: Path) -> dict:
    """Extract holdings data from a PDF factsheet.

    A coordinator + splitter + worker-agent network handles the extraction:
    the PDF is split into small per-scheme (or per-chunk) section PDFs with
    fast PyMuPDF, each section is parsed with pdfplumber in a process pool
    (GIL doesn't serialise the workers), and the results are merged.  If the
    split/pool path is unavailable it falls back to the legacy single-process
    parser below.
    """
    from src.pdf_agents import PdfCoordinatorAgent

    try:
        coordinator = _agent_coordinator()
        result = coordinator.parse(pdf_path, legacy_fallback=lambda: _parse_pdf_legacy(pdf_path))
        if result is not None:
            return result
    except Exception as e:
        logger.warning(f"Agent-network parse failed for {pdf_path.name} ({e}); using legacy")
    return _parse_pdf_legacy(pdf_path)


def _parse_pdf_legacy(pdf_path: Path) -> dict:
    """Original single-process parse (grouped split + generic tables)."""
    from src.pdf_segregator import parse_grouped_pdf

    grouped = parse_grouped_pdf(pdf_path)
    if grouped is not None:
        return grouped

    result = _empty_result(str(pdf_path), "pdf")

    try:
        with pdfplumber.open(pdf_path) as pdf:
            all_text = []
            all_tables = []

            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    all_text.append(text)

                tables = page.extract_tables()
                for table in tables:
                    if table and len(table) > 1:
                        all_tables.append(table)

            result["raw_text"] = "\n\n".join(all_text)
            result["metadata"] = _extract_metadata(result["raw_text"])
            result["raw_tables"] = _clean_tables(all_tables)

            parsed_tables = _classify_tables(result["raw_tables"])
            result["equity_holdings"] = parsed_tables.get("equity", [])
            result["debt_holdings"] = parsed_tables.get("debt", [])
            result["sector_allocation"] = parsed_tables.get("sector", [])
            result["top_holdings"] = parsed_tables.get("top_holdings", [])
            result["cash_allocation"] = parsed_tables.get("cash")

    except Exception as e:
        logger.warning(f"pdfplumber failed on {pdf_path} ({e}), trying PyMuPDF fallback")
        return _parse_pdf_pymupdf(pdf_path, result)

    # Vector-rendered PDFs (text drawn as outlines, e.g. ICICI digital
    # factsheets) have no text layer -> fall back to OCR so holdings are kept.
    if not result["raw_text"].strip() and not result["raw_tables"]:
        ocr_text, tables, ocr_secs = "", [], None

        # Optional AI tier: spend tokens only when the cheap geometry path is
        # missing or looks unreliable (too few rows / invalid weights).
        ai_used = False
        try:
            from src import ai_extract as _ai
            if _ai.is_configured():
                heuristic = None
                try:
                    heuristic = _ocr_pdf_tables(pdf_path, dpi=_load_ocr_dpi())
                except Exception:
                    heuristic = None
                h_rows = (heuristic[1][0].get("rows", [])
                          if heuristic and heuristic[1] else [])
                weak = (len(h_rows) < int(_ai.load_cfg().get("trigger_rows", 12))
                        or not _ai.weights_valid(h_rows))
                if weak:
                    try:
                        ai_rows, ai_meta = _ai.extract_pdf(pdf_path, ocr_text="")
                        if ai_rows and (_ai.weights_valid(ai_rows)
                                        or len(ai_rows) >= 5):
                            headers = ["company", "percent_nav", "isin"]
                            tables = [{
                                "headers": headers,
                                "rows": [dict(zip(headers, [r["company"],
                                                            r["percent_nav"],
                                                            r["isin"]]))
                                         for r in ai_rows]}]
                            result["metadata"]["extraction"] = "ai"
                            result["metadata"]["ai_model"] = ai_meta.get("model")
                            result["metadata"]["ai_seconds"] = ai_meta.get("seconds")
                            ai_used = True
                    except Exception as e:
                        logger.info(f"AI extract unavailable for {pdf_path.name}: {e}")
                else:
                    ocr_text, tables, ocr_secs = heuristic
        except ImportError:
            pass

        if not ai_used and not tables:
            try:
                ocr_text, tables, ocr_secs = _ocr_pdf_tables(pdf_path, dpi=_load_ocr_dpi())
            except Exception as e:
                logger.warning(f"geometry OCR failed on {pdf_path} ({e}); plain OCR")
                ocr_text = _ocr_pdf(pdf_path, dpi=_load_ocr_dpi())
                tables = _tables_from_text(ocr_text)
                ocr_secs = None

        if not ai_used and ocr_text.strip():
            result["raw_text"] = ocr_text
            for k, v in _extract_metadata(ocr_text).items():
                result["metadata"].setdefault(k, v)

        if tables:
            result["raw_tables"] = tables
            parsed_tables = _classify_tables(tables)
            result["equity_holdings"] = parsed_tables.get("equity", [])
            result["debt_holdings"] = parsed_tables.get("debt", [])
            result["sector_allocation"] = parsed_tables.get("sector", [])
            result["top_holdings"] = parsed_tables.get("top_holdings", [])
            result["cash_allocation"] = parsed_tables.get("cash")
            result["metadata"]["ocr"] = True
            if not ai_used:
                result["metadata"]["extraction"] = "ocr"
                if ocr_secs is not None:
                    result["metadata"]["ocr_seconds"] = round(ocr_secs, 2)

    return result


def _ocr_pdf(pdf_path: Path, dpi: int = 200) -> str:
    """Render each page and OCR it with Tesseract (for vector/image-only PDFs)."""
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.warning("OCR deps (PyMuPDF/PIL/pytesseract) not available")
        return ""

    tesseract_cmd = _load_tesseract_cmd()
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    pages_text = []
    try:
        doc = fitz.open(pdf_path)
        try:
            for page in doc:
                pix = page.get_pixmap(dpi=dpi)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                text = pytesseract.image_to_string(img)
                if text.strip():
                    pages_text.append(text)
        finally:
            doc.close()
    except Exception as e:
        logger.warning(f"OCR failed on {pdf_path}: {e}")
        return ""
    return "\n\n".join(pages_text)


def _ocr_pdf_words(pdf_path: Path, dpi: int = 200) -> tuple[str, list[dict], float]:
    """OCR with word boxes: returns (raw_text, word_boxes, ocr_seconds).

    Each word box is ``{"text","left","top","width","height","conf","page"}``.
    Falls back to plain text-only OCR when image_to_data is unavailable."""
    import fitz
    import pytesseract
    from PIL import Image

    tesseract_cmd = _load_tesseract_cmd()
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    all_text: list[str] = []
    words: list[dict] = []
    t0 = time.perf_counter()
    doc = fitz.open(pdf_path)
    try:
        for pno, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            try:
                data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
                n = len(data.get("text") or [])
                for i in range(n):
                    txt = (data["text"][i] or "").strip()
                    if not txt:
                        continue
                    words.append({
                        "text": txt,
                        "left": int(data["left"][i]),
                        "top": int(data["top"][i]),
                        "width": int(data["width"][i]),
                        "height": int(data["height"][i]),
                        "conf": float(data["conf"][i]),
                        "page": pno,
                    })
                page_txt = pytesseract.image_to_string(img)
            except Exception:
                page_txt = pytesseract.image_to_string(img)
            if page_txt.strip():
                all_text.append(page_txt)
    finally:
        doc.close()
    return "\n\n".join(all_text), words, time.perf_counter() - t0


def _rows_from_word_boxes(words: list[dict]) -> list[dict]:
    """Reconstruct holdings rows from OCR word boxes via line/column geometry.

    Words are clustered into visual lines by vertical center, then split into
    x-gap clusters (two-column layouts must not merge). A cluster is a holding
    row when its trailing token is a 0-100 number; everything before it (minus
    rating tokens) is the company name. Section/metric/sector labels are
    filtered with the same sets the agent network uses."""
    from src.pdf_agents import (
        _METRIC_RE, _SECTION_NAME, _SECTION_RE, _SECTOR_RE,
    )

    usable = [w for w in words if w.get("text") and float(w.get("conf") or 0) >= 25]
    if not usable:
        return []
    heights = sorted(w["height"] for w in usable)
    med_h = heights[len(heights) // 2] or 12
    widths = sorted(w["width"] for w in usable)
    med_w = widths[len(widths) // 2] or 40
    y_tol = max(4.0, med_h * 0.6)
    x_split = max(28.0, med_w * 1.6)

    usable.sort(key=lambda w: (w["page"], w["top"], w["left"]))
    lines: list[list[dict]] = []
    for w in usable:
        placed = False
        if lines and lines[-1][-1]["page"] == w["page"]:
            last = lines[-1]
            ref_mid = sum(x["top"] + x["height"] / 2 for x in last) / len(last)
            cur_mid = w["top"] + w["height"] / 2
            if abs(cur_mid - ref_mid) <= y_tol:
                last.append(w)
                placed = True
        if not placed:
            lines.append([w])

    pct_re = re.compile(r"^([0-9]{1,3})(?:[.,]([0-9]{1,3}))?%?$")
    rating_re = re.compile(
        r"^(?:SOV|AAA(?:\+|-)?|AA(?:\+|-)?|A(?:\+|-)?|BBB(?:\+|-)?|BB|B|UNRATED|"
        r"(?:CRISIL|ICRA|CARE|FITCH|BWR|IND)[A-Za-z0-9+\-/]*)$", re.I)

    def _pct_value(tok: str) -> float | None:
        """Parse a percentage token; tolerate OCR-eaten decimal points.

        ``9.14%`` -> 9.14 ; ``136%`` -> 1.36 (dot lost, two decimals assumed)
        ; ``0.99`` -> 0.99. Returns None for anything not plausibly a %NAV."""
        t = tok.strip().rstrip(":;|").strip()
        if t.endswith("%"):
            t = t[:-1].strip()
        m = re.match(r"^([0-9]{1,4})(?:[.,]([0-9]{1,3}))?$", t)
        if not m:
            return None
        whole, frac = m.group(1), m.group(2)
        try:
            if frac is not None:
                v = float(f"{whole}.{frac}")
            elif len(whole) >= 3:
                v = int(whole) / 100.0  # OCR ate the decimal point
            else:
                v = float(whole)
        except ValueError:
            return None
        return v if 0 < v <= 100 else None

    rows: list[dict] = []
    seen: set[tuple] = set()

    def handle_cluster(cluster: list[dict]) -> None:
        toks = [w["text"].strip() for w in sorted(cluster, key=lambda x: x["left"])]
        if len(toks) < 2:
            return
        # attach a standalone '%' to its number ("9.14" "%")
        merged: list[str] = []
        for t in toks:
            if t == "%" and merged:
                merged[-1] += "%"
            else:
                merged.append(t)
        # rightmost percentage-looking token wins; trailing words are junk
        idx = None
        val = None
        for i in range(len(merged) - 1, 0, -1):
            v = _pct_value(merged[i])
            if v is not None:
                idx, val = i, v
                break
        if idx is None:
            return
        name_parts = [t for t in merged[:idx] if not rating_re.match(t)]
        # drop trailing tokens that are themselves percentages ("Retailing 2.72%")
        while name_parts and _pct_value(name_parts[-1]) is not None:
            name_parts.pop()
        name = re.sub(r"\s+", " ", " ".join(name_parts)).strip(" .,-")
        low = name.lower()
        if len(low) < 4:
            return
        if "%" in name or name[-1].isdigit():
            return
        if low in _SECTION_NAME or _METRIC_RE.match(name) \
                or _SECTION_RE.match(name) or _SECTOR_RE.match(name):
            return
        noise = ("annexure", "benchmark", "managing this fund", "as on",
                 "investment horizon", "option", "overall", "refer")
        if any(nz in low for nz in noise):
            return
        key = (low, str(val))
        if key in seen:
            return
        seen.add(key)
        rows.append({"company": name, "percent_nav": f"{val:g}", "isin": ""})

    for line in lines:
        cluster: list[dict] = []
        prev_right = None
        for w in sorted(line, key=lambda x: x["left"]):
            if prev_right is not None and w["left"] - prev_right > x_split:
                handle_cluster(cluster)
                cluster = []
            cluster.append(w)
            prev_right = w["left"] + w["width"]
        handle_cluster(cluster)
    return rows


def _ocr_pdf_tables(pdf_path: Path, dpi: int = 200) -> tuple[str, list[dict], float]:
    """OCR + geometry row reconstruction -> (raw_text, synthetic_tables, ocr_s).

    The synthetic table carries an ``isin`` header so ``_classify_tables``
    routes it to the equity bucket exactly like the other parsers do."""
    raw_text, words, secs = _ocr_pdf_words(pdf_path, dpi=dpi)
    rows = _rows_from_word_boxes(words)
    if not rows:
        # fall back to the legacy regex-on-text scraper
        legacy_rows = _tables_from_text(raw_text)
        if legacy_rows and legacy_rows[0].get("rows"):
            rows = [{"company": r.get("company"), "percent_nav": r.get("percent_nav"),
                     "isin": ""} for r in legacy_rows[0]["rows"]]
    if not rows:
        return raw_text, [], secs
    headers = ["company", "percent_nav", "isin"]
    dict_rows = [dict(zip(headers, [r["company"], r["percent_nav"], r["isin"]]))
                 for r in rows]
    return raw_text, [{"headers": headers, "rows": dict_rows}], secs


def _load_tesseract_cmd() -> str:
    """Read tesseract_cmd from config/settings.yaml (best-effort)."""
    import os

    if os.environ.get("MF_TESSERACT_CMD"):
        return os.environ["MF_TESSERACT_CMD"]
    try:
        import yaml
        from pathlib import Path

        cfg_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            return (cfg.get("parser", {}) or {}).get("tesseract_cmd", "")
    except Exception:
        pass
    return ""


def _load_ocr_dpi() -> int:
    try:
        import yaml
        from pathlib import Path

        cfg_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            return int((cfg.get("parser", {}) or {}).get("ocr_dpi", 150))
    except Exception:
        pass
    return 150


def _load_parser_cfg() -> dict:
    """Read parser.* settings (workers, chunk_pages, ocr)."""
    try:
        import yaml
        from pathlib import Path

        cfg_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            return (cfg.get("parser", {}) or {}) or {}
    except Exception:
        pass
    return {}


def _agent_coordinator() -> PdfCoordinatorAgent:
    cfg = _load_parser_cfg()
    workers = int(cfg.get("workers", 0) or 0)
    chunk_pages = int(cfg.get("chunk_pages", 6) or 6)
    return PdfCoordinatorAgent(
        max_workers=workers or None,
        chunk_pages=chunk_pages,
    )


def _tables_from_text(text: str) -> list[dict]:
    """Best-effort conversion of OCR text into holdings tables.

    Factsheets from vector-rendered PDFs lose the original table geometry, so we
    reconstruct rows from company-name + percentage patterns. This is
    deliberately permissive: the caller's _classify_tables still decides which
    bucket each table belongs to.
    """
    import re

    rows: list[list[str]] = []
    # Holding rows look like "  Company Name Ltd. 1.23%" (optionally preceded by
    # a rating token) with single-space OCR spacing.
    row_re = re.compile(
        r"(?<!\d)([A-Z][A-Za-z0-9&.,'()\- ]{2,}?)\s+"
        r"(?:(?:FITCH|CRISIL|ICRA|CARE|SOV)\s*[A-Za-z0-9+]+(?:\s+[A-Za-z0-9+]+)?\s+)?"
        r"([\d]{1,3}(?:[.,]\d{1,3})?)\s*%"
    )
    seen = set()
    for ln in text.splitlines():
        for m in row_re.finditer(ln):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .")
            pct = m.group(2)
            key = (name.lower(), pct)
            if key in seen:
                continue
            seen.add(key)
            rows.append([name, pct])

    if not rows:
        return []

    cols = max(len(r) for r in rows)
    headers = ["company", "percent_nav"] + ["col%d" % i for i in range(2, cols)]
    headers = headers[:cols]
    dict_rows = [dict(zip(headers, r)) for r in rows]
    return [{"headers": headers, "rows": dict_rows}]


def _empty_result(source_file: str, file_type: str) -> dict:
    return {
        "source_file": source_file,
        "file_type": file_type,
        "metadata": {},
        "equity_holdings": [],
        "debt_holdings": [],
        "sector_allocation": [],
        "top_holdings": [],
        "cash_allocation": None,
        "raw_tables": [],
    }


def _parse_pdf_pymupdf(pdf_path: Path, result: dict) -> dict:
    """Fallback: extract text using PyMuPDF when pdfplumber cannot open the file."""
    try:
        import fitz

        doc = fitz.open(pdf_path)
        try:
            text = "\n\n".join(page.get_text() for page in doc)
        finally:
            doc.close()

        result["raw_text"] = text
        result["metadata"] = _extract_metadata(text)
    except Exception as e:
        logger.error(f"PyMuPDF fallback also failed for {pdf_path}: {e}")
        result["error"] = str(e)

    return result


def _extract_metadata(text: str) -> dict:
    metadata = {}
    nav_match = re.search(r"NAV[:\s]*(?:Rs\.?|INR)?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if nav_match:
        metadata["nav"] = nav_match.group(1).replace(",", "")

    aum_match = re.search(r"AUM[:\s]*(?:Rs\.?|INR)?\s*([\d,]+\.?\d*)\s*(?:Cr|Crore)?", text, re.IGNORECASE)
    if aum_match:
        metadata["aum_cr"] = aum_match.group(1).replace(",", "")

    date_patterns = [
        r"as\s+on\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})",
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            metadata["date"] = match.group(0)
            break

    return metadata


def _clean_tables(raw_tables: list[list[list]]) -> list[dict]:
    cleaned = []
    for table in raw_tables:
        if not table or len(table) < 2:
            continue
        headers = [_clean_cell(h) for h in table[0]]
        rows = []
        for row in table[1:]:
            cleaned_row = [_clean_cell(c) for c in row]
            if any(c for c in cleaned_row):
                rows.append(dict(zip(headers, cleaned_row)))
        if rows:
            cleaned.append({"headers": headers, "rows": rows})
    return cleaned


def _clean_cell(cell) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


def _classify_tables(tables: list[dict]) -> dict:
    result = {"equity": [], "debt": [], "sector": [], "top_holdings": [], "cash": None}

    equity_keywords = {"stock", "equity", "share", "holding", "isin", "quantity", "market value"}
    debt_keywords = {"debt", "bond", "instrument", "coupon", "maturity", "rating", "debenture"}
    sector_keywords = {"sector", "industry", "allocation", "sectoral"}
    top_keywords = {"top 10", "top ten", "top holdings", "major holdings"}
    cash_keywords = {"cash", "cash equivalent", "net current", "treasury"}

    for table in tables:
        header_text = " ".join(h.lower() for h in table["headers"])
        row_text = " ".join(
            " ".join(str(v).lower() for v in row.values())
            for row in table["rows"][:3]
        )
        combined = header_text + " " + row_text

        if any(kw in combined for kw in top_keywords):
            result["top_holdings"].append(table["rows"])
        elif any(kw in combined for kw in sector_keywords):
            result["sector"].append(table["rows"])
        elif any(kw in combined for kw in equity_keywords):
            result["equity"].append(table["rows"])
        elif any(kw in combined for kw in debt_keywords):
            result["debt"].append(table["rows"])
        elif any(kw in combined for kw in cash_keywords):
            if table["rows"]:
                result["cash"] = table["rows"][0]

    return result


def save_parsed_data(data: dict, output_dir: Path, filename_stem: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"{filename_stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"Saved JSON: {json_path}")

    csv_data = _flatten_for_csv(data)
    if csv_data:
        import pandas as pd
        df = pd.DataFrame(csv_data)
        csv_path = output_dir / f"{filename_stem}.csv"
        df.to_csv(csv_path, index=False)
        logger.info(f"Saved CSV: {csv_path}")


def _flatten_for_csv(data: dict) -> list[dict]:
    rows = []
    for table in data.get("equity_holdings", []):
        for row in table:
            row["table_type"] = "equity"
            rows.append(row)
    for table in data.get("debt_holdings", []):
        for row in table:
            row["table_type"] = "debt"
            rows.append(row)
    for table in data.get("sector_allocation", []):
        for row in table:
            row["table_type"] = "sector"
            rows.append(row)
    for table in data.get("top_holdings", []):
        for row in table:
            row["table_type"] = "top_holdings"
            rows.append(row)
    return rows
