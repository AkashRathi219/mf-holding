"""Build the public AMC Report Directory page (W1).

Reads ``config/amc_registry.json`` (maintained by the monthly AMC-direct
link-capture pipeline) and generates a self-contained static page at
``website/amc-directory.html``: one row per AMC with its official
factsheet / monthly-portfolio / fortnightly-portfolio / scheme-wise links.
Empty URL fields render as an em-dash. Zero backend; regenerate after each
monthly capture and re-upload with the rest of the website.

Usage (from repo root)::

    python scripts/build_amc_directory.py
"""
from __future__ import annotations

import html
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "config" / "amc_registry.json"
OUT = ROOT / "website" / "amc-directory.html"

LINK_FIELDS = [
    ("amc_monthly_mf_factsheets", "Factsheets"),
    ("amc_monthly_portfolio_disclosure", "Monthly portfolio"),
    ("amc_fortnightly_portfolio_disclosure", "Fortnightly portfolio"),
    ("scheme_wise", "Scheme-wise"),
]

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FundPulse — AMC Report Directory: official factsheet & portfolio disclosure links for {n} Indian AMCs</title>
<meta name="description" content="Direct links to every Indian mutual fund house's official monthly factsheets, monthly and fortnightly portfolio disclosures, and scheme-wise documents. Generated from the SEBI AMC registry, as of {as_of}.">
<meta name="robots" content="index,follow">
<style>
  :root {{ --bg:#0b1220; --surface:#121a2e; --border:rgba(255,255,255,.09); --text:#e8ecf6;
          --text-2:#9fb0cc; --accent:#5b8cff; --mono:ui-monospace,Consolas,monospace; }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; font-family:'Segoe UI',system-ui,-apple-system,sans-serif; background:
        radial-gradient(1200px 600px at 80% -10%, #16233f 0%, var(--bg) 55%); color:var(--text); }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 60px }}
  h1 {{ font-size:26px; margin:0 0 6px }}
  .sub {{ color:var(--text-2); font-size:14px; margin:0 0 18px; line-height:1.5 }}
  .sub a {{ color:var(--accent); text-decoration:none }}
  #q {{ width:100%; max-width:420px; padding:10px 14px; border-radius:8px; border:1px solid var(--border);
      background:var(--surface); color:var(--text); font-size:14px; margin-bottom:14px }}
  .table-wrap {{ overflow:auto; border:1px solid var(--border); border-radius:10px; background:var(--surface) }}
  table {{ width:100%; border-collapse:collapse; font-size:13.5px; min-width:760px }}
  thead th {{ position:sticky; top:0; background:#0e1730; text-align:left; font-size:11.5px; text-transform:uppercase;
            letter-spacing:.04em; color:var(--text-2); padding:10px 12px; border-bottom:1px solid var(--border); white-space:nowrap }}
  td {{ padding:9px 12px; border-bottom:1px solid var(--border); vertical-align:top }}
  tr:hover td {{ background:rgba(91,140,255,.06) }}
  td.name {{ font-weight:600; white-space:nowrap }}
  td.id {{ color:var(--text-2); font-family:var(--mono); font-size:12px }}
  a.lnk {{ color:var(--accent); text-decoration:none }}
  a.lnk:hover {{ text-decoration:underline }}
  .disclaimer {{ color:var(--text-2); font-size:12.5px; margin-top:16px; line-height:1.55; max-width:900px }}
</style>
</head>
<body>
<div class="wrap">
  <h1>AMC Report Directory</h1>
  <p class="sub">Official disclosure-page links for all {n} SEBI-registered mutual fund houses —
     maintained by the FundPulse monthly link-capture pipeline. As of <b>{as_of}</b>.
     Diagnose any fund's holdings with the <a href="https://YOURAPP.railway.app/register">FundPulse portfolio tools</a>.</p>
  <input id="q" type="search" placeholder="Filter by AMC name…" aria-label="Filter AMCs">
  <div class="table-wrap">
  <table>
    <thead><tr><th>#</th><th>AMC</th><th>Factsheets</th><th>Monthly portfolio</th><th>Fortnightly portfolio</th><th>Scheme-wise</th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
  </div>
  <p class="disclaimer">Links point to each AMC's own website and open in a new tab.
     An em-dash (—) means the AMC has no working link on record yet, not that disclosures don't exist.
     This page is a public index; FundPulse is not affiliated with any asset management company.</p>
</div>
<script>
  document.getElementById('q').addEventListener('input', function(){{
    var q = this.value.toLowerCase();
    document.querySelectorAll('tbody tr').forEach(function(tr){{
      tr.style.display = tr.textContent.toLowerCase().indexOf(q) !== -1 ? '' : 'none';
    }});
  }});
</script>
</body>
</html>
"""


def _cell(url: str | None) -> str:
    url = (url or "").strip()
    if not url:
        return '<td>—</td>'
    return f'<td><a class="lnk" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">open ↗</a></td>'


def build(registry_path: Path = REGISTRY, out_path: Path = OUT,
          app_url: str = "https://YOURAPP.railway.app") -> Path:
    amcs = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    amcs = sorted(amcs, key=lambda a: (a.get("mf_name") or "").lower())
    rows = []
    for i, a in enumerate(amcs, 1):
        cells = "".join(_cell(a.get(f)) for f, _ in LINK_FIELDS)
        rows.append(f'    <tr><td class="id">{i}</td>'
                    f'<td class="name">{html.escape(a.get("mf_name") or "")}</td>{cells}</tr>')
    page = PAGE.format(n=len(amcs), as_of=datetime.now().strftime("%d-%b-%Y"),
                       rows="\n".join(rows), app_url=app_url)
    out_path.write_text(page, encoding="utf-8")
    return out_path


def main() -> int:
    out = build()
    n = len(json.loads(REGISTRY.read_text(encoding="utf-8-sig")))
    print(f"OK: wrote {out.relative_to(ROOT)} ({n} AMCs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
