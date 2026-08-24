# UI Design Reference — welcome-gateway → FundPulse webapp

Single source of truth for the FundPulse visual language, captured from
`docs/internal/welcome-gateway` (TanStack Start + Tailwind v4 + shadcn) and
translated to the plain HTML/CSS/JS webapp (`webapp/static/`).

- Reference project (read-only): `docs/internal/welcome-gateway`
  - Tokens: `src/styles.css` · Spec mirror: `docs/ui-specs/design-system.json`
- Target: `webapp/static/css/style.css` (+ page markup, `js/charts.js`)
- Status: implemented 2026-08. Light + dark themes.

---

## 1. Design tokens (`:root` / `.dark`)

All colors are **oklch**. Never introduce raw hex/rgb in component rules;
always reference a custom property. Full definitions live at the top of
`style.css`. Semantic summary:

| Role | Light | Dark |
|---|---|---|
| Page background | `oklch(0.982 0.003 255)` | `oklch(0.19 0.024 258)` |
| Text | `oklch(0.26 0.03 258)` | `oklch(0.97 0.005 255)` |
| Card / surface | `oklch(1 0 0)` | `oklch(0.235 0.026 258)` |
| Primary (buttons, active states, links) | `oklch(0.34 0.033 258)` dark slate | `oklch(0.78 0.03 254)` light slate |
| Muted text | `oklch(0.56 0.026 258)` | `oklch(0.73 0.026 256)` |
| Border | `oklch(0.905 0.008 256)` | `oklch(1 0 0 / 10%)` |
| Success / Warning / Destructive | `oklch(0.56 0.11 165)` / `oklch(0.55 0.115 75)` / `oklch(0.56 0.19 25)` | lighter variants |
| Sidebar | `oklch(0.24 0.028 258)` | `oklch(0.16 0.022 258)` |
| Focus ring | `oklch(0.44 0.033 259)` | `oklch(0.75 0.031 254)` |

Legacy variable names (`--bg`, `--surface`, `--text-2`, `--accent`, …) are kept
as aliases pointing at the new tokens so JS-generated markup keeps working.

### Gradients & shadows

```css
--gradient-brand: linear-gradient(150deg,
    oklch(0.22 0.026 258) 0%, oklch(0.34 0.033 258) 55%, oklch(0.44 0.033 259) 100%);
--gradient-surface: linear-gradient(180deg, oklch(1 0 0) 0%, oklch(0.972 0.004 255) 100%);
--shadow-card: 0 1px 2px oklch(0.26 0.03 258 / 5%), 0 10px 30px -12px oklch(0.26 0.03 258 / 12%);
--shadow-elevated: 0 24px 60px -24px oklch(0.26 0.03 258 / 28%);
```

(Dark theme redefines all four.)

### Radii

Base `--radius: 12px`, small `--radius-sm: 8px` (inputs/buttons), cards 12–16px,
hero tile 24px, pills 999px.

## 2. Typography & font loading

Google Fonts `<link>` in every page `<head>` (weights are exact):

```
DM Sans 400/500/700 · IBM Plex Mono 400/500/600 · Space Grotesk 500/600/700
```

- Body/UI: **DM Sans**, 14px base, antialiased.
- Headings `h1–h4`: **Space Grotesk**, `letter-spacing: -0.02em`.
- Numbers/data: **IBM Plex Mono** + `font-variant-numeric: tabular-nums`.
- Eyebrow micro-label: `.eyebrow` = mono, 11px, `letter-spacing: .2em`,
  uppercase, muted color.

## 3. Component recipes

### Buttons (`.btn`, variants)

- Base: inline-flex, gap 8px, radius `--radius-sm`, weight 600,
  `transition: background .15s ease, border-color .15s ease, color .15s ease`.
- Primary: `background: var(--primary)`, near-white text, hover darkens
  (`--primary-hover`), `box-shadow: var(--shadow-card)`.
- Outline: 1px `--input` border, card background, hover `--accent` bg.
- Disabled: opacity .55.

### Inputs (`.field input/select/textarea`)

1px `--input` border, radius `--radius-sm`, card background.
Focus: border `--ring` + `box-shadow: 0 0 0 3px var(--ring-soft)` (no outline).

### Cards (`.card`) — “surface-card” treatment

```css
border: 1px solid var(--border);
border-radius: var(--radius);
background-image: var(--gradient-surface); /* subtle top-to-bottom tint */
box-shadow: var(--shadow-card);
```

KPI accent variants keep a 3px top hairline in primary/success/warning.

### Segmented controls (`.nav-tabs`, `.nav-preset`, `.nav-toggle`)

Gateway “tabs” pattern: container `bg-muted` pill (`--secondary`,
radius 10px, 3px padding); active item = card background + soft shadow +
foreground text; idle items muted.

### Tables

Sticky header on `--surface-2`, 11.5px uppercase tracking-wide th, row hover
`--surface-2`, numeric cells `.num` = mono + tabular-nums, right-aligned.

### Badges / chips

Pills radius 999px, tone = soft bg + strong fg pairs
(`blue→accent-soft/accent-dark`, `green`, `amber`, `red`, `grey`).

### Auth split screen (`.auth-wrap`)

Left pane = `var(--gradient-brand)`, near-white text, two blurred glow orbs
(`.auth-side::before/::after`: absolute circles, `rgba(255,255,255,.05)`,
`blur(64px)`), glass stat tiles (below), footer disclaimer.
Right pane = centered column, form card `max-width 420px`, surface-card,
radius 16px, padding 24–28px.

Glass overlay tiers on gradient panes (white alpha over `--gradient-brand`):

| Element | Treatment |
|---|---|
| Stat tile | `rgba(255,255,255,.08)` bg + `backdrop-filter: blur(6px)` + 1px `rgba(255,255,255,.14)` border, radius 12px |
| Logo chip | `rgba(255,255,255,.12)`, radius 10px |
| Tile number | mono, tabular-nums |
| Tile label | 10px uppercase tracking-wider, `rgba(255,255,255,.6)` |
| Badge | `rgba(255,255,255,.12)` pill |

### App shell

- Sidebar: `--sidebar` bg, `--sidebar-border` dividers; links radius 12px,
  padding 10px 12px, idle `--sidebar-foreground` at 75% opacity; active =
  `--sidebar-accent` bg + full fg (NOT blue); hover = `sidebar-accent` 70%.
- Topbar: `position: sticky; top: 0; z-index: 10;` translucent card
  (`--topbar-bg`) + `backdrop-filter: blur(20px)`, bottom border.
  Title = Space Grotesk 20px `-0.02em`. Disclaimer strip = `--accent` 40%
  tint, 11px.
- Drawer: surface gradient, `slidein .18s` keyframe, backdrop `--backdrop`.
- Toast: `--toast-bg` inverted slab, error variant destructive.

### Heatmap (`heat-0…heat-5`)

Monochrome slate ramp toward primary (hue 258), theme-aware via tokens;
`heat-4` uses dark text, `heat-5` white text (light mode; inverted in dark).

## 4. Motion

Micro-transitions only: colors .15s, control states .12s, toast/drawer .2s/
.18s, spinner rotation .7s. Hover lift on feature tiles
(`translateY(-2px)` + shadow deepen). No keyframe libraries.

## 5. Dark mode

- `.dark` class on `<html>`; full token override block in `style.css`.
- Init script (inline, every `<head>`, prevents FOUC): reads
  `localStorage.fea_theme`, falls back to `prefers-color-scheme`.
- Toggle: `.theme-toggle` button (sun/moon glyph), bound by
  `App.initThemeToggle(id)` in `utils.js`; persists choice; dispatches
  `themechange`.
- Canvas charts are theme-aware: `js/charts.js` resolves colors from CSS
  custom properties at draw time (`Charts._c/_resolve/_palette`) and listens
  for `themechange` to re-render.

## 6. Migration map (old → new)

| Legacy (hex / treatment) | Now |
|---|---|
| `--bg #f5f7fa`, system font stack | `--background` token, DM Sans |
| Accent blue `#2456d6/#1b3f9e/#eaf0ff` | slate primary trio (see §1) |
| Auth hero blue gradient `#0e2a6b→…` | `var(--gradient-brand)` + orbs |
| Sidebar `#0e1a33` | `var(--sidebar)` |
| Flat cards + single shadow | gradient-surface + `--shadow-card` |
| Blue active nav pill | `sidebar-accent` slate pill |
| Underline tabs / blue segment actives | segmented muted-pill controls |
| Heat blues `#e7f1ff…#3f7df0` | token heat ramp |
| Toast `#1c2637`, code `#101a2e` | tokenized slabs |
| Hard-coded canvas colors in charts.js | CSS-var resolution at draw time |
| White suggestion dropdowns, `#eef1f5` tracks in app.js | `var(--surface)` / `var(--surface-2)` |

Intentional identity shift: **primary actions are dark slate, not blue**
(matches welcome-gateway). Mid-tone data-viz hues remain for multi-series
legibility.

Documented deviations from the gateway source:
1. Sidebar collapses to an icon rail ≤900px instead of hiding (protects the
   hash-router markup); visually restyled to tokens.
2. Base font stays 14px for table density (gateway default is 16px).
3. `index.html` became a true public landing page (gateway `/`); it no longer
   auto-redirects signed-in users — unknown paths falling back to `/` show the
   landing, not the app.

## 7. Acceptance checklist

- [x] Fonts load on all four pages; headings render Space Grotesk.
- [x] Buttons/links/active-nav are slate, zero legacy blue remains.
- [x] Cards show gradient surface + double shadow; tables keep mono numerals.
- [x] Auth hero shows brand gradient, glow orbs, glass stat tiles (live data).
- [x] Sticky blurred topbar; sidebar matches sidebar tokens.
- [x] Theme toggle works on every page; canvases recolor without reload;
      choice persists and respects OS preference on first visit.
- [x] No functional changes: same IDs, endpoints (`/api/auth/*`),
      hash routing, drawers, toasts.
