"""Stock identity resolver: maps the equity ISINs used by the app to NSE symbols.

Sources (in priority order)
---------------------------
1. **Manual override** — drop a CSV at ``data/raw/stock_manual/identity.csv`` with
   columns ``isin,symbol,name``; anything here wins.
2. **NSE equity master** — ``archives.nseindia.com/content/equities/EQUITY_L.csv``
   (SYMBOL … ISIN NUMBER) for every NSE-listed equity.
3. **Nifty constituents** — ``data/nifty/constituents/*.csv`` (ISIN Code + Symbol).
4. **Name fallback** — if no symbol resolves, ``data/reference/equity_isins.csv``
   name is kept so the record is still listed (price/actions/reports simply won't
   run for it).

Output: ``data/stocks/identity.json`` = ``{ ISIN: {symbol, name, source} }`` for
every ``confirmed_equity=1`` security.

Run::

    python -m src.stock_identity
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

from .stock_common import EQUITY_ISINS_CSV, IDENTITY_JSON, NIFTY_CONSTITUENTS_DIR, STOCKS_DIR, http_get

NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"


def _clean_symbol(s: str) -> str:
    return (s or "").strip().upper()


def load_manual_identity() -> dict[str, dict]:
    path = Path("data/raw/stock_manual/identity.csv")
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            isin = (r.get("isin") or "").strip().upper()
            if isin:
                out[isin] = {"symbol": _clean_symbol(r.get("symbol")),
                             "name": (r.get("name") or "").strip(), "source": "manual"}
    return out


def load_nse_equity_list() -> dict[str, dict]:
    """ISIN -> {symbol, name} from NSE's equity master (EQUITY_L.csv)."""
    out: dict[str, dict] = {}
    try:
        raw = http_get(NSE_EQUITY_LIST_URL, timeout=45)
        text = raw.decode("latin-1")
        for line in text.strip().splitlines():
            parts = line.split(",")
            if len(parts) < 7 or parts[0].strip().lower() == "symbol":
                continue
            symbol = _clean_symbol(parts[0])
            name = parts[1].strip()
            isin = (parts[6] or "").strip().upper()
            if isin and symbol:
                out[isin] = {"symbol": symbol, "name": name, "source": "nse_equity_list"}
    except Exception:
        pass  # unreachable — constituents + manual still apply
    return out


def load_nifty_constituents() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not NIFTY_CONSTITUENTS_DIR.is_dir():
        return out
    for csv_path in sorted(NIFTY_CONSTITUENTS_DIR.glob("*.csv")):
        try:
            with open(csv_path, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    isin = (r.get("ISIN Code") or "").strip().upper()
                    symbol = _clean_symbol(r.get("Symbol"))
                    if isin and symbol:
                        out.setdefault(isin, {"symbol": symbol,
                                              "name": (r.get("Company Name") or "").strip(),
                                              "source": "nifty_constituents"})
        except Exception:
            continue
    return out


def load_equity_isins() -> list[dict]:
    rows = []
    if not EQUITY_ISINS_CSV.exists():
        return rows
    with open(EQUITY_ISINS_CSV, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append({"isin": (r.get("isin") or "").strip().upper(),
                         "name": (r.get("name") or "").strip(),
                         "confirmed_equity": (r.get("confirmed_equity") or "").strip()})
    return rows


def build_identity(force: bool = False) -> dict[str, dict]:
    STOCKS_DIR.mkdir(parents=True, exist_ok=True)
    if not force and IDENTITY_JSON.exists():
        return load_identity()

    manual = load_manual_identity()
    nse = load_nse_equity_list()
    nifty = load_nifty_constituents()

    identity: dict[str, dict] = {}
    for row in load_equity_isins():
        isin = row["isin"]
        if row.get("confirmed_equity") != "1":
            continue  # only pure listed stocks
        if isin in manual:
            identity[isin] = manual[isin]
        elif isin in nse:
            identity[isin] = nse[isin]
        elif isin in nifty:
            identity[isin] = nifty[isin]
        else:
            identity[isin] = {"symbol": "", "name": row["name"], "source": "name_only"}
    save_identity(identity)
    return identity


def load_identity() -> dict[str, dict]:
    if not IDENTITY_JSON.exists():
        return build_identity(force=True)
    with open(IDENTITY_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def save_identity(identity: dict[str, dict]) -> None:
    STOCKS_DIR.mkdir(parents=True, exist_ok=True)
    IDENTITY_JSON.write_text(json.dumps(identity, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    import click

    @click.command()
    @click.option("--force", is_flag=True, help="Rebuild even if identity.json exists")
    def _cli(force):
        ident = build_identity(force=force)
        with_sym = sum(1 for v in ident.values() if v.get("symbol"))
        click.echo(f"Stock identity: {len(ident)} confirmed-equity ISINs, "
                   f"{with_sym} with an NSE symbol")
        click.echo(f"  -> {IDENTITY_JSON}")

    return _cli()


if __name__ == "__main__":
    sys.exit(main())