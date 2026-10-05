"""Final coverage verification for the Jun-Sep 2026 window."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

B = Path(r"D:\opencode\Digital Assets\Fundpulse\mf_holding")
reg = json.loads((B / "config/amc_registry.json").read_text(encoding="utf-8"))
amcs = [e["mf_name"] for e in reg if e.get("mf_name")]
safe = lambda n: n.replace(" ", "_").replace("/", "-")  # noqa: E731
W = ["2026-06", "2026-07", "2026-08", "2026-09"]
NEED = set(W)

MONTHS = (("Jan", "01"), ("Feb", "02"), ("Mar", "03"), ("Apr", "04"),
          ("May", "05"), ("Jun", "06"), ("Jul", "07"), ("Aug", "08"),
          ("Sep", "09"), ("Oct", "10"), ("Nov", "11"), ("Dec", "12"))


def mo(d: str) -> str:
    if len(d) == 10 and d[4] == "-":
        return d[:7]
    for name, ym in MONTHS:
        if f"-{name}-" in d:
            return f"2026-{ym}"
    return ""


print("=" * 66)
print("FINAL COVERAGE  -  Jun-Sep 2026 window")
print("=" * 66)

for lab, root in (("RAW   ", B / "data/raw/pdfs"),
                  ("PARSED", B / "data/parsed/amc_websites")):
    print(f"\nMUTUAL FUNDS - {lab}")
    tot = 0
    for m in W:
        y, mm = m.split("-")
        have = n = 0
        miss = []
        for a in amcs:
            d = root / safe(a) / y / mm
            c = len([f for f in d.rglob("*") if f.is_file()]) if d.exists() else 0
            n += c
            if c:
                have += 1
            else:
                miss.append(a.split()[0])
        tot += n
        tail = f"   missing: {','.join(miss)}" if miss else ""
        print(f"   {m}: {have:2d}/57 AMCs  {n:6d} files{tail}")
    print(f"   WINDOW TOTAL: {tot} files")

print("\nSTOCKS - daily price history")
sh = B / "data/stock_history"
files = sorted(sh.glob("*.json"))
full = part = none = 0
for f in files:
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        none += 1
        continue
    ms = {mo(h.get("date", "")) for h in (d.get("history") or [])}
    if NEED <= ms:
        full += 1
    elif ms & NEED:
        part += 1
    else:
        none += 1
print(f"   {len(files)} ISINs | all 4 months: {full} | partial: {part} | none: {none}")

print("\nSTOCKS - corporate actions")
print(f"   {len(list((B / 'data/stock_actions').glob('*.json')))} ISIN files")

print("\nBONDS")
bm = B / "data/bond_market/raw"
days = sorted([d.name for d in bm.iterdir() if d.is_dir()])
c = Counter(d[:7] for d in days)
print("   day-snapshots  Jun {0} | Jul {1} | Aug {2} | Sep {3}".format(
    c.get("2026-06", 0), c.get("2026-07", 0),
    c.get("2026-08", 0), c.get("2026-09", 0)))
cat = B / "data/bond_market/bond_catalog.json"
if cat.exists():
    j = json.loads(cat.read_text(encoding="utf-8"))
    print(f"   catalog as_of={j.get('as_of')}  bonds={j.get('n_bonds')}")

print("\nMUTUAL FUNDS - NAV history")
nh = B / "data/nav_history"
win = 0
for f in nh.glob("*.json"):
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        continue
    pts = d if isinstance(d, list) else (d.get("history") or d.get("points") or [])
    if any(mo(x.get("date", "")) in NEED for x in pts if isinstance(x, dict)):
        win += 1
print(f"   {len(list(nh.glob('*.json')))} schemes | with Jun-Sep data: {win}")

print("\nINTEGRITY (post-load audit)")
rep = sorted((B / "data/reports").glob("integrity_*.json"))[-1]
d = json.loads(rep.read_text(encoding="utf-8"))
tiers = json.loads((B / "data/reference/integrity_tiers.json").read_text(encoding="utf-8"))
hist = Counter((v.get("tier") if isinstance(v, dict) else v) for v in tiers.values())
print(f"   report: {rep.name}")
print(f"   schemes audited : {d.get('n_schemes')}")
print(f"   holding rows    : {d.get('n_holding_rows')}")
for k in sorted(hist, key=str):
    print(f"   {k}: {hist[k]}")
print(f"   flags: {d.get('flags')}")

print("\nWEBAPP DB (frontend-visible)")
db = sqlite3.connect(f"file:{B / 'data/webapp.db'}?mode=ro", uri=True)
print(f"   schemes : {db.execute('SELECT COUNT(*) FROM schemes').fetchone()[0]}")
print(f"   holdings: {db.execute('SELECT COUNT(*) FROM holdings').fetchone()[0]}")
db.close()