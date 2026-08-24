"""Phase 3: routing 404 panel [U2], MF-A2 stale badge [U3], masking leaks [U4L].

Frontend behaviour is asserted statically (source-level contracts) plus one
real unit test of the server-side staleness flag; JS syntax is checked with
`node --check` when node is available so a broken bundle fails CI.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "webapp" / "static" / "js" / "app.js"
UTILS_JS = ROOT / "webapp" / "static" / "js" / "utils.js"
APP_HTML = ROOT / "webapp" / "static" / "app.html"
DATA_HEALTH = ROOT / "webapp" / "data_health.py"


def test_js_bundles_parse():
    if not shutil.which("node"):
        pytest.skip("node not available for JS syntax check")
    for f in (UTILS_JS, APP_JS):
        r = subprocess.run(["node", "--check", str(f)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name} has a syntax error:\n{r.stderr}"


# ---- U2: unknown hash routes to an explicit 404 panel -----------------------

def test_unknown_hash_panel_exists():
    html = APP_HTML.read_text(encoding="utf-8")
    assert 'id="screen-notfound"' in html, "missing #screen-notfound section"
    assert 'id="notfoundHash"' in html, "panel must echo the offending hash"
    js = APP_JS.read_text(encoding="utf-8")
    assert "screens[hash]" in js and "screen-notfound" in js
    assert "screens[hash] || screens.schemes" not in js, \
        "silent fallback must be gone"


# ---- U4L: every render path masks the aggregator source key -----------------

def test_source_filter_dropdown_masked():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'App.sourceLabel(s)' in js, "schemeSource filter must mask labels"
    assert 'l: s.replace(/_/g, " ")' not in js.split("schemeCoverage")[0].split(
        "schemeSource")[-1], "raw underscore-replace still used for sources"


def test_confidence_tooltip_masked_and_stale_line():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'App.sourceLabel(cf.source)' in js
    assert '"Source: " + ((cf && cf.source)' not in js, \
        "confBadge tooltip leaks the raw source key"
    assert "cf.stale" in js, "badge must surface the stale marker"


def test_admin_reliance_table_masked():
    js = APP_JS.read_text(encoding="utf-8")
    assert '${App.esc(w.source || "none")}' not in js, \
        "reliance table renders raw source key"
    # the by-source rollup must go through the label map too — raw keys
    # (e.g. the aggregator archive key) must never render [U4L/D2]
    assert "${App.esc(App.sourceLabel(src))}" in js, \
        "by-source table renders raw source keys"
    assert "App.esc(src)" not in js, "unmasked raw source render found"


def test_utils_js_single_mask_entrypoint():
    utils = UTILS_JS.read_text(encoding="utf-8")
    # the internal aggregator key exists only as a map entry; the user-facing
    # label attributes the data to the AMC [D2] — never the third-party name
    assert "advisorkhoj:" in utils and 'advisorkhoj: "AMC"' in utils
    assert utils.count("App.maskedSource = App.sourceLabel;") == 1


# ---- U3: server-side staleness flag -----------------------------------------

def test_scheme_confidence_flags_stale():
    from webapp.data_health import scheme_confidence

    old = scheme_confidence({"coverage": "has_holdings", "source": "amfi",
                             "as_of": (date.today() - timedelta(days=200))
                             .isoformat()})
    fresh = scheme_confidence({"coverage": "has_holdings", "source": "amfi",
                               "as_of": (date.today() - timedelta(days=10))
                               .isoformat()})
    undated = scheme_confidence({"coverage": "has_holdings", "source": "amfi",
                                 "as_of": ""})
    assert old["stale"] is True and old["age_days"] > 180
    assert fresh["stale"] is False
    assert undated["stale"] is False  # unknown age is not proof of staleness


def test_data_health_constant_matches_badge_copy():
    src = DATA_HEALTH.read_text(encoding="utf-8")
    assert "ADVISORKHOJ_STALE_DAYS = 180" in src
