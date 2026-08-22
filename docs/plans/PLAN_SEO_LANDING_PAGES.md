# Plan — SEO Landing Pages (ghost pages for search/AI discovery)

Status: ✅ BUILT (3 pages in `website/`) · Created: 22-Aug-2026
Related: `PLAN_TRY_APP.md` (funnel target), `../internal/COMPLIANCE_CHECKLIST.md` (copy guardrails)

## Goal

Standalone, self-contained HTML pages to be hosted on the **main website
infrastructure but deliberately unlinked from it** ("ghost pages"), designed so
search engines *and* AI answer engines (ChatGPT, Perplexity, Google AI
Overviews) can discover and quote them.

Decisions locked:
- **Ghost-page strategy**: the three pages are NOT linked from the main site's
  navigation/footer/breadcrumbs. They cross-link each other as an intentional
  cluster; discovery comes from XML-sitemap submission, direct URL sharing
  (WhatsApp/Reddit) and external backlinks only.
- Keep webapp `/` as the login redirect; pages live on the main website only.
- Three pages: adviser product landing + data-methodology + retail investors page.
- Visual: match the webapp dark-auth theme (navy gradient + blue accent).
- Feedback stays an in-app widget (separate follow-up, see bottom).
- URLs: placeholders until provided — find-and-replace before publishing.

---

## Placeholders (replace before publish)

| Placeholder | Used for |
|---|---|
| `https://www.YOURSITE.com` | canonical URLs, OG/Twitter URLs, sitemap |
| `https://YOURAPP.railway.app/login` · `/register` · `/data-methodology` | CTA buttons and cross-links |

---

## File 1 — `product-landing.html`

Fully self-contained: inline CSS/JS, no external dependencies, works on any host.

### Head / SEO layer
- Unique `<title>` (e.g. "Factsheet Engine AI — Mutual Fund Holdings Analytics for Indian Advisers")
- Meta description (~155 chars), keywords, `robots=index,follow`
- Canonical → main-site URL; Open Graph + Twitter card tags
  - WhatsApp-grade OG rules: image 1200×630 JPEG/PNG over HTTPS, absolute URL,
    key content center-safe (WhatsApp center-crops); tags must be in the initial
    HTML — crawlers do not execute JS
- JSON-LD blocks:
  - `SoftwareApplication` (applicationCategory FinanceApplication, offers)
  - `Organization`
  - `FAQPage` (mirrors the on-page FAQ verbatim)

### Sections (in order)
1. Sticky nav — anchor links + **Sign in** button → `<webapp>/login`
2. Hero — H1 value prop; primary CTA → `<webapp>/register`; secondary link → methodology page
3. Trust bar — static dataset stats with as-of date
   (250k+ holding rows · 50+ AMCs · ~98% coverage). Live stats via
   `/api/scope-stats` would need CORS on the webapp for the main-site origin — optional follow-up.
4. Features grid — mirrors real functionality:
   - Scheme Explorer (holdings + per-scheme confidence badges)
   - Overlap & concentration diagnostics
   - Security Directory (prices, dividends/splits, results announcements)
   - NSE Bonds (coupon/YTM/maturity)
   - Model Portfolios + rule-based compliance engine
   - White-label Proposal Generator
5. "Why fund-holdings data is hard" — aggregation-challenge narrative:
   fragmented AMC PDF disclosures, format drift, Direct≡Regular dedupe,
   index/ETF benchmark mapping, ISIN gaps
6. "Our data-integrity rules" — stated as claims of the mechanism:
   source priority `amfi > amc_website > advisorkhoj > index`,
   per-scheme confidence scoring tiers, refresh cadence
   (daily NAV/stocks/bonds, monthly holdings), explicit gap disclosure
7. How-it-works — sign up → analyze → generate client proposal
8. Who-it's-for — RIAs · RAs · MFDs
9. FAQ — answer-shaped Q&A (also in JSON-LD)
10. Final CTA band + SEBI-style disclaimer footer

### Handoff block (HTML comment at top of file)
Paste-ready snippets for the main website:
- `robots.txt`: allow Googlebot/Bingbot + explicitly GPTBot, ClaudeBot,
  PerplexityBot, Google-Extended, CCBot
- `<sitemap>` entry template
- `llms.txt` block summarizing product + links to both pages

### Compliance copy guardrails
- Factual stats only, each with as-of date; no performance/return claims anywhere
- Never self-label "SEBI-compliant"; allowed phrasing: "built for SEBI-registered advisers"
- Disclaimer visible: diagnostic tool for factual analysis only — not investment advice

---

## File 2 — `data-methodology.html`

Same visual shell, self-contained. Deep-dive of landing sections 5–6:
- The aggregation challenge in detail (three sources, priority order, matching logic)
- Confidence scoring model & tiers (high ≥80 / medium ≥55 / low <55 / grey no-data)
- Freshness & cadence tables
- Known-gap disclosure philosophy
- Cross-links from/to landing page
- JSON-LD: `Dataset` + `FAQPage`; own title/description/canonical

---

## Design tokens (from `webapp/static/css/style.css`)

| Token | Value |
|---|---|
| Hero/auth gradient | `linear-gradient(160deg,#0e2a6b 0%,#1b3f9e 55%,#2456d6 100%)` |
| Sidebar navy | `#0e1a33` |
| Accent | `#2456d6` (hover `#1b3f9e`) |
| Status palette | green `#16845c` · amber `#b7791f` · red `#c0392b` (+ `-soft` bgs) |
| Font | system stack (-apple-system/Segoe UI/Roboto…), mono accents |
| Logo mark | 📊 |

---

## File 3 — `investors.html` (retail ghost page)

Same visual shell, retail tone. Built ahead of the trial app (PLAN_TRY_APP.md);
presents the coming-soon free portfolio X-ray and captures a waitlist.

- **Head/SEO**: title/description targeting investor queries ("mutual fund
  overlap checker", "what stocks do my mutual funds hold"), canonical
  `/investors`, OG/Twitter, JSON-LD: `WebApplication` + `FAQPage`
- **Sections**: hero ("You own six funds. You might really own thirty stocks")
  → hidden-overlap problem (3 cards) → how-it-works 3 steps (CAS download →
  upload → report) → privacy-by-design band (in-memory parse, auto-delete,
  PAN never stored, 24h expiring links) → what-the-report-shows cards →
  waitlist form (stub JS; TODO wire to real endpoint) → FAQ → final CTA +
  full disclaimer footer
- **Compliance copy guardrails** (same as adviser pages): diagnostics-only
  language, explicit "not investment advice" answers in FAQ, factual dataset
  stats with as-of dates

---

## Verification checklist (all three pages)

- [x] Both files open standalone in browser (file:// and http://)
- [x] All JSON-LD blocks parse as valid JSON
      (landing: Organization/SoftwareApplication/FAQPage · methodology:
      Dataset/FAQPage · investors: WebApplication/FAQPage)
- [x] Single H1 per page; canonical + OG/Twitter tags present
- [x] FAQ schema text mirrors visible text verbatim on every page
- [x] HTML tag balance clean
- [ ] Every CTA/anchor resolves after placeholder replacement
- [ ] OG images meet 1200×630 HTTPS spec once real assets exist
- [ ] Responsive at mobile width (spot-check)

## Queued follow-ups (later tasks)

1. In-app feedback widget + auto-prompt in webapp (`app.js`, posts to existing
   `/api/feedback`) — approved earlier.
2. Optional: enable CORS on webapp for main-site origin to power live scope-stats.
3. Swap URL placeholders once production domains are provided.
4. Wire investors-page waitlist form to a real endpoint when the try-app lands.
