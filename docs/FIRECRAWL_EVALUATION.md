# Firecrawl Evaluation — Fit for the MF Data Pipeline

Assessment of [firecrawl/firecrawl](https://github.com/firecrawl/firecrawl) against
our fetching/downloading/parsing stack, with a phased adoption plan.
Reference date: Sep 2026. Sources: repo README, self-host guide, pricing page.

---

## 1. What Firecrawl provides

| Capability | Endpoint | What it does |
|---|---|---|
| Scrape | `POST /v2/scrape` | Any URL → markdown / HTML / links / screenshot / structured JSON (schema) |
| Map | `POST /v2/map` | Discover all URLs on a site, optionally filtered (`search=`), instant |
| Crawl | `POST /v2/crawl` | Scrape all pages of a site (async job + polling) |
| Batch scrape | `POST /v2/batch_scrape` | Thousands of URLs asynchronously |
| Search | `POST /v2/search` | Web search with full page content |
| Interact / Agent | `/v2/scrape/{id}/interact`, `/v2/agent` | Browser actions, prompt-driven navigation/extraction (**Cloud-only**) |
| Monitor | Cloud product | Re-checks pages on a schedule, deterministic extraction (**Cloud-only**); 1 credit/page/check, JSON-style checks 7 credits/page |
| Media parsing | via scrape | Web-hosted PDF/DOCX → clean text/markdown |
| Anti-bot | managed (Cloud) / Fire-engine (self-host, separate) | Rotating proxies, JS-heavy rendering, rate-limit orchestration |

Python SDK: `firecrawl-py` (MIT). Core engine: AGPL-3.0.

### Cloud vs self-host (material differences)

| | Cloud | Self-host (Docker Compose) |
|---|---|---|
| Core scrape/crawl/map/search | Managed | Included (Fetch + Playwright) |
| Actions / screenshots / Interact / Agent / Monitor | Included | **Not in default stack** (actions need Fire-engine) |
| LLM-backed JSON formats | Managed | Requires your own OpenAI-compatible provider/Ollama |
| Advanced anti-bot (Fire-engine) | Managed | Run/configure separately — not included |
| Infra | Zero | API + workers + Playwright + Redis + RabbitMQ + Postgres (Docker) |
| Auth/TLS/persistence/upgrades | Firecrawl | You own them |

### Pricing snapshot (effective Sep 2026)

- Free: **1,000 credits/month**, 2 concurrent, low rate limits (10 req/min scrape/map/search).
- Hobby: $16–19/mo → 5,000 credits; Standard: $83/mo → 100,000 credits.
- Credits: scrape/crawl/map **1/page**, search 2/10 results, JSON format **+4 credits/page**, interact 2/credit per browser-minute. Failed requests free except 403/404 responses (1 credit).

---

## 2. Fit matrix — our pipeline vs Firecrawl

| Our stage | Current approach | Firecrawl fit | Verdict |
|---|---|---|---|
| AMCFI NAV (portal.amfiindia.com) | `urllib`, 90-day windows, resumable staging | No gain; portal is a form endpoint, not a page crawl | **Keep** |
| AMFI JSON APIs (otherdata, TER, SIF) | httpx against internal APIs | Firecrawl scrapes pages, not internal JSON endpoints | **Keep** |
| AMC discovery (57 AMCs) | 20 bespoke adapters + Generic/Playwright Hybrid; registry URLs go stale | `/v2/map` + `/v2/scrape` (links format) can discover portfolio/factsheet docs on JS-heavy pages; managed proxies | **Adopt as fallback** (Phase 1/2) |
| AMC downloads (PDF/XLS/ZIP) | Direct download, sha256 provenance, cached raw corpus | Firecrawl returns rendered/cleaned content, not the raw bytes we hash; downloader is already reliable | **Keep direct download** |
| Holdings table extraction | pdfplumber / PyMuPDF / OCR / sha256 cache | Firecrawl markdown/JSON is lossy for exact %NAV/ISIN tables; would weaken provenance | **Keep** (only consider for HTML factsheet pages) |
| Scheme metadata miners (descriptions, fund managers, riskometer) | Local corpus mining | Could scrape live AMC pages instead — but we already own the source docs | **No change** |
| NSE prices / actions / announcements | Cookie-warmed `urllib`, bulk archive files, curl_cffi | Undocumented APIs + archive CSVs, not pages; proxying through a third party adds ToS/policy risk and zero reliability gain | **Keep** |
| NSE result PDFs → statements | Direct download + pdfplumber + AI tier | Same provenance problem; our NSE session already works | **Keep** |
| Bond bulk files + live API | `urllib` archive files, cookie-warmed API | Bulk CSV/JSON, not page crawling | **Keep** |
| Registry URL discovery (`amfi-directory`) | AMFI portal RSC scrape | Fine already; Firecrawl `/v2/search` could fill gaps for the 4 URL-less AMCs | **Optional Phase 2** |
| New AMC onboarding | Hand-write adapter/Playwright selectors | `/v2/map` + `links` gets to a working discovery in one adapter, no selectors | **Best use case** |

Bottom line: Firecrawl is a **discovery/extraction transport for AMC websites**,
not a replacement for the AMFI/NSE bulk-data plumbing. The core holdings chain
(direct download → hash → parse) should stay as-is.

---

## 3. Proposed integration

### 3.1 `FirecrawlAdapter` — third fallback for AMC discovery

A new adapter next to Generic/Playwright (`src/amc_adapters/firecrawl.py`):

```text
HybridAdapter:
  GenericAdapter (httpx, free)
  → PlaywrightAdapter (local Chrome, free, selector-free)
  → FirecrawlAdapter (managed infra, credits)   ← new, only when the first two
                                                   found no dated documents
```

- Implementation: call `/v2/scrape` on the portfolio/factsheet page with
  `formats: ["links"]` (or `/v2/map` with `search="portfolio"`), filter
  `.pdf/.xlsx/.xls/.csv/.zip` links, feed them into the existing
  `document_type`/month detection (`base.extract_month_year`) so the rest of the
  pipeline (download → sha256 → parse) is untouched.
- Config: `FIRECRAWL_API_KEY` for Cloud, or `FIRECRAWL_API_URL` for self-host;
  optional per-AMC switch in `config/amc_registry.json`
  (`"discovery": "firecrawl"`).
- Cost control: only invoked when Generic + Playwright yield zero dated links
  (today's Hybrid condition), so typical usage is a handful of pages/month.
- Prefer `httpx` calls against `/v2/*` over adding `firecrawl-py` — we already
  ship httpx and keep the slim-deps boot graph untouched.

### 3.2 Discovery for registry gaps (optional)

For the 4 URL-less AMCs (Carnelian, Lakshya, Monarch, Nuvama) + ASK/AlphaGrep:
`/v2/search` or `/v2/map` to locate the **official** AMC disclosure page, then
store it in `config/amc_registry.json` as `amfi_directory_url`-style provenance
(never overwrite curated URLs). Fetching still happens directly from the AMC.

### 3.3 What we deliberately do NOT route through Firecrawl

- AMFI NAV/JSON APIs, NSE APIs, NSE/Firecrawl archives (no page to crawl; our
  session handling is already solved).
- Holdings-table parsing (raw-byte provenance + exact table math required).
- Daily jobs (Firecrawl adds latency/credits/coupling for zero benefit).

### 3.4 Optional later: Monitor

Cloud `Monitor` could watch the ~57 AMC disclosure pages and tell us when a new
monthly portfolio publishes (today handled by retrying days 1–5). Cost ≈ 1
credit/page/check + 7 credits/page for structured checks — roughly
57 × 8 ≈ 456 credits per full sweep, viable on free/Hobby tier. Only worth it
if the retry-window method ever misses publications.

---

## 4. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Cost creep (credits, +4/page JSON) | Fallback-only invocation; no JSON format in Phase 1; free tier + monthly usage report |
| Third-party transit of AMC content (Cloud) | Self-host if policy requires; keep direct downloads for raw files; Firecrawl respects robots.txt by default |
| Self-host ops burden (Redis/RabbitMQ/Postgres/Fire-engine) | Not on Railway free tier; run on workstation only if Cloud is disallowed. AGPL-3.0: review with legal before shipping as a network service |
| Advanced anti-bot not included self-hosted | Hard AMCs (HDFC/Edelweiss) are already solved with curl_cffi/AES; keep bespoke adapters |
| Loss of raw-document provenance | Firecrawl used for **link discovery only**; documents still downloaded and hashed by `pdf_downloader`/`batch_parser` |
| Dependency/scope creep in webapp | Adapter lives in `src/` pipeline only; webapp boot graph unchanged |

---

## 5. Phased rollout

| Phase | Scope | Exit criteria |
|---|---|---|
| 0 — Evaluate (free) | Manually run Firecrawl Cloud `/v2/map` + `/v2/scrape` against the AMCs that currently fail Hybrid (the 4 URL-less + ASK/AlphaGrep, Kotak/ICICI if stale) | Working doc links found for ≥2 previously failing AMCs within free credits |
| 1 — Fallback adapter | `src/amc_adapters/firecrawl.py` wired as the last Hybrid fallback, env-gated, telemetry via `refresh_log` | A monthly run discovers+downloads 100% of links for a test AMC with zero adapter-specific code; no change to existing adapters' results |
| 2 — Registry discovery | `/v2/search` assist for missing disclosure URLs; provenance recorded | Registry gaps filled or explicitly documented as no-disclosure |
| 3 — Optional Monitor | Watch AMC disclosure pages; trigger link-capture job on change | Detects a new monthly publication before the days 1–5 retry loop would |

Decision gate: if Phase 0 shows no benefit on the failing AMCs (anti-bot is
rarely the blocker — stale URLs and bespoke JS interactions usually are), stop
at Phase 0 and keep the current stack.

---

## 6. Relation to the data-source map

This document complements [data_sources/README.md](data_sources/README.md):
Firecrawl would appear there as an **access layer for AMC website discovery
only**, with the upstream source list and frequencies unchanged. If adopted,
add `firecrawl` to the "Ways data points are obtained" table and record
per-AMC discovery provenance in `data/logs/portfolio_links/<YYYY-MM>.json`.
