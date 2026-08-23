# Documentation Index

Everything documented lives here (`docs/`). Root keeps only `README.md` and
`DIRECTION.md`. Build plans from product sessions live in `plans/`.

> `docs/internal/` is **git-ignored by design** — business-sensitive material
> (regulatory gap lists, strategy scratch) never enters the repo.

## Orientation

| Doc | What it covers |
|---|---|
| [`../README.md`](../README.md) | Project overview, quick start, layout |
| [`../DIRECTION.md`](../DIRECTION.md) | Master direction & data map (the hub doc) |

## Technical references

| Doc | What it covers |
|---|---|
| [DATA_SOURCES_RESEARCH.md](DATA_SOURCES_RESEARCH.md) | Audit of holdings/%NAV sources; pipeline decisions |
| [SCHEME_DETAILS_STRATEGY.md](SCHEME_DETAILS_STRATEGY.md) | Authoritative spec for the Scheme Details drawer |
| [ANALYSIS_RESULTS.md](ANALYSIS_RESULTS.md) | How the compliance report is calculated |
| [DATA_CADENCE.md](DATA_CADENCE.md) | Refresh schedules (holdings/NAV/stocks/bonds) |
| [DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md) | Railway + R2 deployment & redeploy runbook |

## Feature docs

| Doc | What it covers |
|---|---|
| [MODEL_PORTFOLIOS.md](MODEL_PORTFOLIOS.md) | Model portfolios, strategies, deployment |
| [CLIENT_PORTFOLIOS.md](CLIENT_PORTFOLIOS.md) | Client portfolios feature |
| [DATA_HEALTH_PLAN.md](DATA_HEALTH_PLAN.md) | Data-health score + refresh telemetry design |

| [DECISIONS.md](DECISIONS.md) | Append-only product/engineering decision log |
| [NAMING_BRAND_ARCHITECTURE.md](NAMING_BRAND_ARCHITECTURE.md) | Product naming (Pulse family), brand critique, subdomain architecture |

## Ops trackers

| Doc | What it covers |
|---|---|
| [APP_REVIEW_ACTIONS.md](APP_REVIEW_ACTIONS.md) | Review action points & status |
| [plans/EXECUTION_TRACKER.md](plans/EXECUTION_TRACKER.md) | Living task status — parsed by `scripts/tracker_status.py` |

## Build plans (`plans/`) — approved roadmaps

| Plan | Status |
|---|---|
| [PLAN_SEO_LANDING_PAGES.md](plans/PLAN_SEO_LANDING_PAGES.md) | ✅ BUILT — see `website/` |
| [PLAN_TRY_APP.md](plans/PLAN_TRY_APP.md) | READY TO BUILD — retail trial micro-app (CAS upload → report card → WhatsApp loop) |
| [PLAN_TASK_BACKLOG.md](plans/PLAN_TASK_BACKLOG.md) | AUDIT 23-Aug-2026 — incomplete tasks & priority order |
| [PLAN_PERFORMANCE_ANALYTICS.md](plans/PLAN_PERFORMANCE_ANALYTICS.md) | QUEUED — rolling returns / Sharpe / Sortino / IR engine |

## Website deliverables (`../website/`) — GHOST pages for search/AI discovery

> **Ghost-page strategy:** these three pages are deliberately NOT linked from
> aracharatventures.com navigation. They form their own cross-linked cluster and
> are discovered only via XML sitemap submission, direct URL shares
> (WhatsApp/Reddit) and external backlinks. Deployment notes are in the HTML
> comment at the top of each file.

| File | What it is |
|---|---|
| [`product-landing.html`](../website/product-landing.html) | Adviser-facing product landing (SEO + AI-answer-engine optimised; retail teaser section) |
| [`data-methodology.html`](../website/data-methodology.html) | Public data-methodology deep-dive (Dataset + FAQPage schema) |
| [`investors.html`](../website/investors.html) | Retail-investor page for the future free portfolio X-ray (waitlist CTA; WebApplication + FAQPage schema) |

> All three are self-contained HTML with placeholders (`YOURSITE.com`, `YOURAPP.railway.app`)
> and a paste-ready robots/sitemap/llms.txt handoff block in an HTML comment at the top.
> Replace placeholders before publishing.

## Internal only (untracked)

| File | What it covers |
|---|---|
| `internal/COMPLIANCE_CHECKLIST.md` | SEBI/DPDP/CERT-In/data-licensing findings & gap list |
| `internal/DATA_SUMMARY_UI_PROMPT.md` | Dataset summary + UI-generation prompt scratch |
