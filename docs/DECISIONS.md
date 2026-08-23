# Decision Log

Append-only record of product/engineering decisions. Newest last.
Task-level status lives in [`plans/EXECUTION_TRACKER.md`](plans/EXECUTION_TRACKER.md).

## 2026-08-23 · D1 — Orphaned screens PARKED
Proposal Generator, Portfolio Tools (overlap) and API & Mapping are fully built but
unrouted (`app.html:301/:236/:369`; absent from the `screens` map at `app.js:3-9`).
Decision: **do nothing now** — parked on the review list for later. Dashboard
hiding remains intentional (commit `2a90567`). Revisit trigger: Try App launch or
next UX pass.

## 2026-08-23 · D2 — Advisorkhoj stays hidden + AMC Report Directory
1. **Source masking stays policy.** "advisorkhoj" continues to display as
   "AMC disclosure" (`utils.js:57`). Fix the one leak where the raw string appears
   as a filter option (`app.js:82`). Freshness flagging (MF-A2) proceeds without
   naming the source.
2. **New build task:** public **AMC Report Directory** — one page linking every AMC
   to its official website and report/factsheet download pages, generated from
   `config/amc_registry.json` (maintained by the monthly AMC-direct link-capture
   pipeline). Static HTML, no backend; ships with the website cluster (task W1).
