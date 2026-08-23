# Naming & Brand Architecture

> Status: PROPOSED — agreed direction (Pulse family + subdomain hosting), not yet
> applied to the codebase. Decision log entry pending in [`DECISIONS.md`](DECISIONS.md).
>
> Context: Aracharat Ventures portfolio analysis session, 23-Aug-2026.
> Covers three products: MF holdings analytics (this repo), PwD accessibility
> audit platform, and IoT sensor monitoring platform — all hosted as subdomains
> of **aracharatventures.com** (registered, currently on `dns-parking.com` NS).

---

## 1. Current name — "Factsheet Engine AI" (MF holdings analytics)

### 1.1 Brand critique

**Strengths**

- Descriptive & SEO-friendly: "factsheet" is literally what AMCs publish.
- "AI" is honest — `src/ai_extract.py` really uses a vision LLM (OpenRouter)
  to parse factsheets.
- "Engine" signals aggregation at scale (250k+ rows, 50+ AMCs).

**Weaknesses**

- Too generic to trademark or recall; sounds like an internal tool ("Engine"
  is dev-speak). Says nothing about *what* or *for whom*.
- **"AI" is a credibility risk**: the methodology page sells trust and
  verifiability to compliance-minded RIAs/RAs, yet the name leads with the
  buzzword most associated with hallucination. The differentiator is *verified
  data*, not AI (AI is 1 of 3 data sources — see `DIRECTION.md` priority order).
- Scope mismatch: product also covers AMFI disclosures and NSE bonds;
  "Factsheet" narrows that.

### 1.2 UI consistency audit (exact string is consistent everywhere)

| # | Issue | Location |
|---|-------|----------|
| 1 | Three different JSON-LD entity names → search engines may treat them as distinct products | `website/product-landing.html`, `website/investors.html` ("— Investor Portfolio X-Ray"), `website/data-methodology.html` ("— Indian MF Portfolio Holdings Dataset") |
| 2 | Internal doc calls it "MF Holdings Aggregator", not Factsheet Engine AI | `DIRECTION.md` |
| 3 | Title-tag pattern varies: bare / "— Analytics" / "Adviser registration · …" (name-last) | `webapp/static/login.html`, `app.html`, `register.html` |
| 4 | Inconsistent User-Agent formats | `webapp/amfi_fetch.py` vs `src/fetch_missing_nav.py` |
| 5 | Brand lockup is a generic 📊 emoji on every surface | all brand bars |
| 6 | Unshipped placeholders `YOURSITE.com` / `YOURAPP.railway.app` incl. og:image URLs | all 3 `website/` pages |

---

## 2. Product portfolio & chosen family

All three products are the same species: **measure → reveal → monitor**.
Shared suffix compounds brand equity across the portfolio.

| Family | MF holdings | Accessibility audit | IoT monitoring |
|--------|-------------|--------------------|----------------|
| …Scope | SchemeScope | AccessScope | ClimateScope *(rejected — too climate-specific)* |
| …Lens | FundLens | AccessLens | EnviroLens |
| **…Pulse** ✅ | **FundPulse** | **AccessPulse** | **SensePulse** |

**Decision:** **Pulse family.**

- FundPulse — what your funds really hold
- AccessPulse — how accessible your product really is
- SensePulse — what your machines really sense

Narrative: *"Every signal that matters, on one pulse."*

Notes:

- IoT product is variable-agnostic monitoring (hardware sensors → data +
  control via dashboards), so **SensePulse** was chosen over NodePulse
  (hardware-forward) and EdgePulse (control-loop-forward); "sense" covers
  hardware, data and control forever.
- Accessibility-audit runner-ups: AccessLens, BarrierZero, A11yAudit,
  EnableScan, EveryUser, SightLine, EqualEntry, IncluCheck, OpenPath, AssureA11y.
- IoT runner-ups: SenseGrid, ThermoTrace, AmbientIQ, HygroSense, ClimaWatch,
  EnvSentry, MicroMeter, AirSignal, DewPoint.
- If "AI" must be kept anywhere: use it as a trust badge
  ("AI-parsed · human-verifiable"), never in the brand name.

---

## 3. Hosting architecture — aracharatventures.com subdomains

| Platform | Subdomain |
|----------|-----------|
| MF holdings analytics | `fundpulse.aracharatventures.com` |
| PwD accessibility audit | `accesspulse.aracharatventures.com` |
| IoT sensor monitoring | `sensepulse.aracharatventures.com` |
| Corporate / umbrella site | `www` / root `.aracharatventures.com` |

Branding rules:

- Product name stands alone in the UI (logo reads "FundPulse"); footer carries
  *"A product of Aracharat Ventures"*. Keeps freedom to move any product to its
  own .com later without UI rebrand.
- Per-subdomain isolation is a plus: localStorage tokens (`fea_token`) and
  cookies stay sandboxed per platform.
- `website/` ghost pages live under the corporate domain
  (`aracharatventures.com/product` etc.) linking out to each subdomain.

DNS wiring: point registrar NS → Cloudflare, CNAME each subdomain → its Railway
service custom domain (see [`DEPLOY_RAILWAY.md`](DEPLOY_RAILWAY.md)).

---

## 4. Rename touch-points (Factsheet Engine AI → FundPulse)

When executing the rename, update:

- [ ] `webapp/static/*.html` — `<title>` tags + `.brand` bars (+ CSS logo swap 📊 → brand mark)
- [ ] `webapp/main.py` — FastAPI title, report footer `_Generated on … - Factsheet Engine AI_`
- [ ] `webapp/static/js/utils.js`, `app.js`, login/register JS — optional `fea_token` key migration (needs versioned re-auth fallback)
- [ ] `website/*.html` — titles, og:*, JSON-LD names (standardize to one entity + `alternateName`), disclaimers, © lines
- [ ] `README.md`, `DIRECTION.md` headers; `scripts/restart_webapp.ps1` comment
- [ ] User-Agents in `webapp/amfi_fetch.py`, `webapp/amfi_portal.py`, `src/fetch_missing_nav.py`
- [ ] Replace `YOURSITE.com` / `YOURAPP.railway.app` placeholders with real subdomains

Pre-commit checks: trademark screen classes 9/42 (existing "pulse" brands:
PulseSecure in networking, various health-tech), domain/social-handle availability.
