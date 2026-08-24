"""UI design-system port (welcome-gateway -> webapp) [UI-DS].

Static source-contract tests: the design language ported from
docs/internal/welcome-gateway must be present in webapp/static, legacy
hard-coded colors must be gone, and all functional hooks (form ids, API
endpoints, hash-router markup) must survive untouched. JS syntax is checked
with `node --check` when node is available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "webapp" / "static"
CSS = STATIC / "css" / "style.css"
APP_JS = STATIC / "js" / "app.js"
CHARTS_JS = STATIC / "js" / "charts.js"
UTILS_JS = STATIC / "js" / "utils.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---- JS bundles still parse --------------------------------------------------

def test_js_bundles_parse():
    if not shutil.which("node"):
        pytest.skip("node not available for JS syntax check")
    for f in (APP_JS, CHARTS_JS, UTILS_JS):
        r = subprocess.run(["node", "--check", str(f)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name} has a syntax error:\n{r.stderr}"


# ---- tokens + gateway signature utilities in style.css -----------------------

def test_css_has_gateway_tokens():
    css = _read(CSS)
    for token in ("--gradient-brand", "--gradient-surface", "--shadow-card",
                  "--shadow-elevated", "--font-display", "--font-sans",
                  "--chart-grid", "--canvas-bg", "--topbar-bg"):
        assert token in css, f"missing token {token}"
    assert ".dark {" in css, "dark theme token block missing"
    assert ".surface-card" in css and ".eyebrow" in css
    assert ".theme-toggle" in css
    # oklch color space adopted
    assert css.count("oklch(") > 50


def test_css_legacy_palette_gone():
    css = _read(CSS)
    for legacy in ("#2456d6", "#1b3f9e", "#0e1a33", "#0e2a6b", "#f5f7fa",
                   "#e8edf4", "#1c2637", "#101a2e", "#3f7df0"):
        assert legacy.lower() not in css.lower(), f"legacy hex {legacy} still in style.css"


def test_auth_glass_tiles_live_in_css_not_inline():
    css = _read(CSS)
    for cls in (".scope-tile", ".scope-num", ".scope-lbl", ".scope-badge"):
        assert cls in css
    login = _read(STATIC / "login.html")
    assert "<style>" not in login, "login.html inline styles must stay in style.css"
    assert "backdrop-filter" in css


# ---- pages: fonts, theme init, cache-bust ------------------------------------

@pytest.mark.parametrize("page", ["index.html", "login.html", "register.html", "app.html"])
def test_page_head_contract(page):
    html = _read(STATIC / page)
    assert "fonts.googleapis.com/css2?family=DM+Sans" in html, f"{page}: font link missing"
    assert 'localStorage.getItem("fea_theme")' in html, f"{page}: theme init script missing"
    assert "style.css?v=4" in html, f"{page}: stylesheet not cache-busted to v=4"
    assert "?v=3" not in html, f"{page}: stale ?v=3 asset reference remains"


@pytest.mark.parametrize("page", ["index.html", "login.html", "register.html", "app.html"])
def test_theme_toggle_present(page):
    html = _read(STATIC / page)
    assert 'id="themeToggle"' in html, f"{page}: theme toggle button missing"


# ---- functional invariants preserved -----------------------------------------

def test_login_functional_hooks_intact():
    html = _read(STATIC / "login.html")
    for needle in ('id="loginForm"', 'id="loginError"', 'id="scopeGrid"',
                   'App.api("/auth/login"', 'localStorage.setItem("fea_token"',
                   'id="scSchemes"', "/api/scope-stats"):
        assert needle in html, f"login.html lost functional hook: {needle}"


def test_register_functional_hooks_intact():
    html = _read(STATIC / "register.html")
    for needle in ('id="regForm"', 'id="regError"', 'id="scopeStats"',
                   'App.api("/auth/register"', 'class="scope-tile"',
                   "/api/scope-stats"):
        assert needle in html, f"register.html lost functional hook: {needle}"


def test_app_shell_structure_intact():
    html = _read(STATIC / "app.html")
    for sid in ("screen-dashboard", "screen-schemes", "screen-bonds", "screen-securities",
                "screen-overlap", "screen-compare", "screen-proposal", "screen-models",
                "screen-api", "screen-admin", "screen-notfound", "screen-schemedetail",
                "screen-secdetail"):
        assert f'id="{sid}"' in html, f"app.html lost screen panel {sid}"
    for needle in ('id="nav"', 'id="drawer"', 'id="toast"', 'id="userBox"',
                   "js/charts.js?v=4", "js/app.js?v=5"):
        assert needle in html


# ---- charts.js is theme-aware -------------------------------------------------

def test_charts_resolve_tokens_at_draw_time():
    js = _read(CHARTS_JS)
    assert "_resolve" in js and "_c(" in js and "_palette" in js
    assert '"--primary"' in js and '"--chart-grid"' in js and '"--canvas-bg"' in js
    assert 'document.addEventListener("themechange"' in js
    # line/gauge/nav charts resolve color per draw, not per mount
    # (_renderLine + _renderGauge use this._resolve; mountNavChart uses Charts._resolve)
    assert (js.count('Charts._resolve(opts.color)')
            + js.count('this._resolve(opts.color)')) >= 3
    # no hard-coded legacy canvas colors remain
    for legacy in ("#2456d6", "#e8edf4", "#17202f"):
        assert legacy not in js, f"charts.js still hard-codes {legacy}"


def test_utils_has_theme_api():
    js = _read(UTILS_JS)
    assert "App.initThemeToggle" in js and "fea_theme" in js
    # [D2] masking contract untouched
    assert "App.sourceLabel" in js and "advisorkhoj" in js


# ---- app.js de-hard-coding -----------------------------------------------------

def test_appjs_no_theme_breaking_literals():
    js = _read(APP_JS)
    assert 'background:#fff' not in js, "white dropdown surfaces break dark mode"
    assert '#eef1f5' not in js and '#e8eaed' not in js, "light tracks break dark mode"
    for var_use in ("var(--surface)", "var(--surface-2)", "var(--heat-5)", "var(--warning)"):
        assert var_use in js
    assert "@primary" in js and "@success" in js, "series colors should use token refs"
    # functional guards untouched
    assert "screens[hash]" in js and "App.sourceLabel(s)" in js


# ---- landing page ------------------------------------------------------------

def test_landing_page_structure():
    html = _read(STATIC / "index.html")
    for needle in ("lp-hero", "lp-tile", "lp-features", 'href="/register"',
                   'href="/login"', "/api/scope-stats", "lp-stats"):
        assert needle in html, f"landing page missing {needle}"


# ---- detail pages rework [UI-DETAIL] -----------------------------------------

def test_scheme_detail_card_layout():
    js = _read(APP_JS)
    for marker in ('id="schemeAnalytics"',           # analytics hook preserved
                   ">Scheme facts<",                  # facts card
                   ">Asset mix<",                     # mix card (absorbed summary)
                   "data-hold-panel",                 # segmented holdings tabs
                   ".hold-tabs .tab",
                   "tr[data-mix]",                    # mix rows jump to class tab
                   'kpi("Latest NAV"',                # KPI strip
                   '"Expense ratio"'):
        assert marker in js, f"scheme detail rework missing: {marker}"
    # duplicated bottom summary table must be gone (folded into Asset mix)
    assert "<th>Summary</th>" not in js, "old duplicate Summary table still present"
    # old loose kv/plan-table blocks replaced
    assert 'class="data plan-table"' not in js, "orphaned plan-table still present"


def test_scheme_analytics_table_layout():
    js = _read(APP_JS)
    for marker in ('>Returns <span class="badge grey">CAGR</span>',
                   "retRow(", "wStart(", "wEnd(",
                   ">Risk &amp; benchmark<",
                   "riskRow(",
                   "Not enough NAV history for risk analytics yet",
                   '"schemeRollChart"'):
        assert marker in js, f"analytics rework missing: {marker}"
    # every return row carries its exact window dates
    assert js.count('c.since_inception_window') >= 1 and "partial" in js


def test_security_detail_card_layout():
    js = _read(APP_JS)
    for marker in (">Security facts<", ">Price history (daily close)<",
                   "toggleSecPane", 'id="sec-pane-actions"',
                   ">Top weighted in schemes", "nav-chart-price"):
        assert marker in js, f"security detail rework missing: {marker}"


def test_appjs_cache_bumped():
    html = _read(STATIC / "app.html")
    assert "app.js?v=5" in html, "app.js not cache-busted after detail rework"
    assert "?v=3" not in html
