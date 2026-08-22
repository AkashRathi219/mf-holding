"""Nifty benchmark-weight ingestion for index/ETF funds.

Produces ``data/nifty/weights.json``:  { index_name: { isin: weight_pct } }
which the webapp's data layer applies to index schemes by their ``index_name``.

Sources (in priority order, tried automatically):
  1. A full-weight file the user drops in (CSV/JSON/Excel with symbol/isin/weight) —
     from the niftyindices.com "Market Capitalisation & Weightage" monthly report or
     the NSE ``equity-stockIndices`` API (full, accurate).
  2. The official Nifty factsheet PDFs on niftyindices.com (reachable, but only the
     top constituents carry explicit weights; the remainder are equal-weighted).

Run:  python -m webapp.nifty_weights
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
NIFTY_DIR = BASE_DIR / "data" / "nifty"
CONST_DIR = NIFTY_DIR / "constituents"
OUT = NIFTY_DIR / "weights.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0"}

# our internal index name -> factsheet PDF code (ind_<code>.pdf)
# (codes verified against live niftyindices.com factsheet URLs)
CODE_MAP = {
    "NIFTY_50": "nifty50",
    "NIFTY_100": "nifty_100",
    "NIFTY_Next_50": "next50",
    "NIFTY_Smallcap_100": "niftysmallcap100",
    "NIFTY_Smallcap_250": "nifty_smallcap_250",
    "Nifty_Midcap_150": "nifty_midcap_150",
    "Nifty_Bank": "nifty_bank",
    "Nifty_IT": "nifty_it",
    # The following strategy/thematic indices have no reachable factsheet PDF in
    # the ind_*.pdf pattern -> they fall back to equal weight (or a --file ingest).
    "NIFTY_LargeMidcap_250": None,
    "Nifty_Capital_Markets": None,
    "Nifty200_Momentum_30": None,
    "Nifty200_Alpha_30": None,
    "Nifty_Oil_and_Gas_Index": None,
}

_WS = re.compile(r"\s+")


def norm(name: str) -> str:
    return _WS.sub(" ", (name or "")).strip().lower()


def load_constituents() -> dict[str, list[tuple[str, str]]]:
    """Return { normalized_index_name: [(company, isin)] } from data/nifty/constituents/*.csv."""
    out: dict[str, list[tuple[str, str]]] = {}
    if not CONST_DIR.is_dir():
        return out
    for p in sorted(CONST_DIR.glob("*.csv")):
        rows = []
        try:
            with open(p, encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    parts = line.rstrip("\n").split(",")
                    if i == 0 or len(parts) < 5:
                        continue
                    rows.append((parts[0].strip(), parts[4].strip().upper()))
        except Exception:
            continue
        if rows:
            out[p.stem] = rows
    return out


def extract_pdf_weights(code: str, constituents: list[tuple[str, str]]) -> dict[str, float]:
    """Download ind_<code>.pdf and extract top-constituent weights by matching the
    known company names to the weight that follows them in the text."""
    url = f"https://www.niftyindices.com/Factsheet/ind_{code}.pdf"
    r = httpx.get(url, timeout=45, headers=UA, follow_redirects=True)
    if r.status_code != 200 or r.content[:4] != b"%PDF":
        return {}
    try:
        import pdfplumber
    except ImportError:
        return {}
    text = ""
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        for p in pdf.pages:
            text += "\n" + (p.extract_text() or "")
    weights: dict[str, float] = {}
    for company, isin in constituents:
        c = norm(company)
        if not c:
            continue
        # match by the first two significant tokens (e.g. "hdfc bank")
        toks = re.findall(r"[a-z]+", c)
        if len(toks) < 2:
            continue
        pat = re.compile(r"\b" + re.escape(toks[0]) + r"[\s&]+" + re.escape(toks[1]) +
                         r"[^\d]*?(\d{1,2}\.\d{1,2})", re.IGNORECASE)
        m = pat.search(text)
        if m:
            weights[isin] = float(m.group(1))
    return weights


def build_weights(indices: list[str] | None = None) -> dict[str, dict[str, float]]:
    """Build { index: { isin: weight } } — real top weights from the factsheet PDF,
    equal-weighted for the tail so each index sums to ~100%."""
    const = load_constituents()
    if indices is None:
        indices = [k for k in CODE_MAP if k in const]
    out: dict[str, dict[str, float]] = {}
    for idx in indices:
        code = CODE_MAP.get(idx)
        rows = const.get(idx) or []
        if not code or not rows:
            continue
        w = extract_pdf_weights(code, rows)
        if not w:
            continue
        # assign equal weight to any remaining constituents
        missing = [isin for _, isin in rows if isin not in w]
        known_sum = sum(w.values())
        tail = (100.0 - known_sum) / len(missing) if missing else 0.0
        for isin in missing:
            w[isin] = round(tail, 4)
        out[idx] = w
    return out


def load_full_weights_file(path: Path, index_col: str = "index",
                           isin_col: str = "isin", weight_col: str = "weight") -> dict:
    """Parse a user-provided full-weight CSV/JSON/Excel into the weights.json format."""
    import pandas as pd
    out: dict[str, dict[str, float]] = {}
    if path.suffix.lower() in (".json",):
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    if path.suffix.lower() in (".csv", ".xlsx", ".xls"):
        df = pd.read_excel(path) if path.suffix.lower() != ".csv" else pd.read_csv(path)
        for _, row in df.iterrows():
            idx = str(row.get(index_col) or "").strip()
            isin = str(row.get(isin_col) or "").strip().upper()
            w = row.get(weight_col)
            if not idx or not isin or w is None:
                continue
            out.setdefault(idx, {})
            if isin.startswith("INE"):
                out[idx][isin] = float(w)
    return out


def save(weights: dict) -> Path:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(weights, indent=1, ensure_ascii=False), encoding="utf-8")
    return OUT


def main() -> int:
    import click

    @click.command()
    @click.option("--file", "file_path", type=click.Path(exists=True), default=None,
                  help="Full-weight CSV/XLSX/JSON to ingest instead of live fetch")
    @click.option("--index", "indices", multiple=True,
                  help="Limit to specific index names (repeatable); default: all known")
    def _cli(file_path, indices):
        if file_path:
            weights = load_full_weights_file(Path(file_path))
            src = "file"
        else:
            weights = build_weights(indices or None)
            src = "niftyindices factsheet"
        if not weights:
            click.echo("No weights obtained. Provide a full-weight --file or retry.", err=True)
            return 1
        path = save(weights)
        total = sum(len(v) for v in weights.values())
        click.echo(f"Ingested {len(weights)} indices / {total} securities (source: {src}) -> {path}")
        return 0

    return _cli()


if __name__ == "__main__":
    sys.exit(main())
