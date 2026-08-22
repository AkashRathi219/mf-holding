# Data cadence — the two dates every scheme carries

For **every scheme** the system tracks two important dates:

1. **Latest NAV value date** — the date of the most recent daily NAV.
   - Refreshed **every day** (AMFI daily NAV feed → `data/nav_history/<code>.json`).
   - Surfaced in the Scheme Explorer drawer as **"Latest NAV date (daily)"** and on the
     portfolio holding statement as the **NAV date** column.

2. **Portfolio holdings announcement date** — the as-of date of the scheme's monthly
   portfolio disclosure (the date the holdings were published by the AMC).
   - Announcements are treated as a **weekly** update cycle.
   - Surfaced as **"Holdings as-of (weekly announcement)"** in the Scheme Explorer
     drawer and as the `as_of` on each scheme / holdings snapshot.

## Where each is stored

| Date | Field | Source | Cadence |
|---|---|---|---|
| Latest NAV | `nav_date` (scheme detail), `nav_history` last date | AMFI NAV history (`src.nav_daily`, `src.nav_freshness`) | Daily |
| Holdings announcement | `as_of` / `holdings_date` on the scheme | Parsed AMC monthly portfolio / advisorkhoj | Weekly (announcement) |

## Operations

- `python main.py nav-daily` — appends today's NAVs (daily refresh).
- `python main.py nav-backfill --cas|--all|--codes` — pulls NAVs from AMFI up to the
  latest date when a scheme's history is stale.
- `python main.py nav-status` — reports which schemes are complete from inception to
  the latest NAV date.
- `python main.py nav-freshness` — checks for stale NAVs/prices; `--backfill` refreshes them.