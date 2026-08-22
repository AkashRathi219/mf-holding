# Plan — Retail Trial App ("Try It") — Phases 1–3

Status: APPROVED, ready to build · Created: 22-Aug-2026
Related: `PLAN_SEO_LANDING_PAGES.md` (funnel destination), `../internal/COMPLIANCE_CHECKLIST.md`

## Goal

A small public-facing app that lets **retail investors** try the engine —
upload a CAS statement, get an instant portfolio report card, share it via
WhatsApp/Reddit — creating a viral top-of-funnel that converts to the adviser
product.

Decisions locked:
- CAS parsing: **server-side** with strict auto-delete (not browser-side).
- Scope: full Phase 1–3 build.
- Compliance bright line: factual diagnostics only — never advice
  ("72% overlap across your funds" ✅ · "consider selling X" ❌).

## Market validation (research summary)

| Player | Model | Signal |
|---|---|---|
| casanalyser.com | Upload CAS → analysis, "No login required / we do NOT store" | privacy positioning works |
| theindianfirecalculator.com | CAS importer parsing in-browser | client-side parse is proven (we chose server-side anyway) |
| Arthavi / INDmoney / Kuvera | Demo portfolios + CAS import funnels | demo-entry is standard |
| casparser (GitHub, MIT) | Robust parser for CAMS/KFintech/NSDL/CDSL incl. PAN passwords | optional dependency if homegrown parser stalls |

Key insight: CAS PDFs are PAN-encrypted (password = PAN). Server-side parsing
means transiently handling PANs → the privacy mechanics below are mandatory,
and the page copy must state them explicitly.

Distribution:
- **WhatsApp (primary)**: OG tags must be in initial HTML (crawler runs no JS);
  1200×630 HTTPS JPEG; personal-trust channel = high CTR; Hindi/regional share
  copy lifts conversions 25–40%.
- **Reddit (secondary)**: r/IndiaInvestments removes self-promo from low-history
  accounts; 90/10 participation rule; warm an account, ask mods first, launch in
  promo-friendly subs only.

---

## Phase 1 — Analyze flow

### Backend: new `webapp/try_api.py` (mounted in `main.py`)

**`POST /api/try/analyze`** — public, NO auth, multipart:
- Fields: `file` (CAS PDF), `password` (PAN), `consent=true` (required)
- Guards: size cap 10MB; per-IP sliding-window rate limit (~5 uploads/hr/IP);
  global daily cap via env var (protect Railway resources)
- Parse fully **in memory**: PyMuPDF `authenticate(password)` → refactor
  `_pdf_cas_items()` out of `tools_api.py` into shared helper accepting bytes+password
- Resolve schemes via existing `resolve_scheme_ids()` matcher →
  `portfolio_analysis()` → confidence badges via `data_health.scheme_confidence`
- Report payload: asset split, top holdings, overlap pairs, concentration flags,
  scheme count, AMC count
- Return `{report_id, report}` where `report_id = secrets.token_urlsafe(16)`

**Privacy mechanics (non-negotiable):**
- File buffer + password discarded immediately post-parse
- Never written to disk; never logged (log event counts only)
- Report snapshot cached in-memory keyed by `report_id`, **TTL 24h**, purge task

**`GET /api/try/report/{rid}`** — snapshot fetch; `410 Gone` when expired.

### Frontend: new `webapp/static/try.html`
- Hero ("See what's really inside your mutual fund portfolio") + disclaimers
- Upload dropzone → password field (*"used only to open your file in memory — never stored"*)
- Mandatory DPDP consent checkbox (itemized purpose + deletion promise)
- Analysis progress states → rendered report card reusing app styling/charts
- Route registered in `_PAGES` as `"try"`

---

## Phase 2 — Viral loop

- **`GET /try/{rid}`** — server-rendered HTML (dynamic route, not static file):
  OG/Twitter meta present in the *initial HTML* (WhatsApp requirement),
  canonical from `SITE_URL` placeholder
- **`GET /try/{rid}/card.jpg`** — Pillow-generated **1200×630** navy-gradient
  share card showing aggregate stats ONLY (no names/PAN/values)
- Share row on report: Copy-link button + `wa.me` intent link with pre-filled text

## Phase 3 — Funnel & feedback

- CTA band on every report → adviser landing page
  ("Advisers: run this across your whole client book")
- Inline feedback form on try page → existing `/api/feedback` with `context:"try"`
- Optional email-capture for "full report" later

---

## Verification checklist

- [ ] Restart via `scripts/restart_webapp.ps1`; health OK
- [ ] End-to-end: upload → analyze → report → share link → card image
- [ ] Expiry: snapshot returns 410 after TTL; rate limiter blocks burst
- [ ] OG image meets spec; test real WhatsApp preview on device
- [ ] Confirm no PDF bytes/password ever hit logs or disk
- [ ] Copy review: no advice-flavoured language anywhere

### Open inputs needed
- One real CAS PDF as test fixture (none exists in repo — only parsed JSON sample)
- Production URL for `SITE_URL` placeholder

## Queued follow-ups
- Entity compliance checklist (`docs/internal/COMPLIANCE_CHECKLIST.md`)
- In-app adviser feedback widget (separate task)
- Performance metrics engine (`PLAN_PERFORMANCE_ANALYTICS.md`) — natural Phase 4+
  extension of the trial app once scheme analytics exist
