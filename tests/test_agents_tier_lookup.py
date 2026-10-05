"""[T36] Tier lookup / UI surfacing contract tests (SPEC §11.0 / AC-21).

Every test writes its own sidecar fixture into ``tmp_path`` and points
:func:`src.agents.tier_lookup.load_tiers` / ``lookup`` at it explicitly —
the real ``data/reference/integrity_tiers.json`` is never written (a guard
test asserts its stat is unchanged) and the webapp DB is never opened.

Covers: a known key returns the stored record verbatim; a missing key
returns ``None``, which displays as ``"Unknown"`` and is never a
complete-looking label; the empty-``as_of`` ``|unknown`` bucket resolves
(including non-date ``as_of`` values, mirroring the writer); the §11.3
display-label mapping (T0/T1/T2/T3/None) and the ``is_full_portfolio``
template gate; missing/malformed sidecars degrading to ``{}``/``None``
without raising; and the default-path wiring resolving against the working
directory.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.agents.tier_lookup import (
    DATA_REFERENCE,
    DEFAULT_TIERS_PATH,
    UNKNOWN_MONTH,
    display_label,
    is_full_portfolio,
    load_tiers,
    lookup,
    tier_key,
)

AMC = "Test AMC Mutual Fund"
FUND = "Test Top Ten Fund"


def _record(**overrides) -> dict:
    record = {
        "tier": "TOP10_FALLBACK",
        "tier_code": "T1",
        "document_class": "factsheet_topn",
        "coverage_pct": 45.0,
        "n_holdings": 10,
        "escalate": False,
        "flags": [],
        "reason": "factsheet_topn: 10 rows in top-N band 8-12 (~10), coverage 45.00%",
        "source": "advisorkhoj",
        "as_of": "2026-07-31",
    }
    record.update(overrides)
    return record


def _write_sidecar(root: Path, entries: dict) -> Path:
    path = root / "reference" / "integrity_tiers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1: a known key returns the stored record verbatim
# ---------------------------------------------------------------------------

def test_lookup_returns_the_stored_record_for_a_known_key(tmp_path):
    entry = _record()
    path = _write_sidecar(tmp_path, {f"{AMC}|{FUND}|2026-07": entry})
    record = lookup(AMC, FUND, "2026-07-31", path=path)
    assert record == entry
    assert record["tier"] == "TOP10_FALLBACK"
    assert record["tier_code"] == "T1"
    assert record["document_class"] == "factsheet_topn"
    assert record["coverage_pct"] == 45.0
    assert record["n_holdings"] == 10
    assert record["escalate"] is False
    assert record["source"] == "advisorkhoj"


# ---------------------------------------------------------------------------
# 2: a missing key returns None — explicitly never a "complete" label
# ---------------------------------------------------------------------------

def test_missing_key_returns_none_never_a_complete_label(tmp_path):
    path = _write_sidecar(tmp_path, {f"{AMC}|{FUND}|2026-07": _record()})
    record = lookup(AMC, "Test Absent Fund", "2026-07-31", path=path)
    assert record is None
    assert lookup(AMC, FUND, "2026-08-31", path=path) is None
    label = display_label(record)
    assert label == "Unknown"
    assert label != "Full portfolio"
    assert "complete" not in label.lower()
    assert is_full_portfolio(record) is False


# ---------------------------------------------------------------------------
# 3: empty / non-date as_of resolves via the |unknown bucket
# ---------------------------------------------------------------------------

def test_empty_as_of_resolves_via_the_unknown_bucket(tmp_path):
    entry = _record(
        tier="ESCALATE", tier_code="T2", document_class="unknown",
        coverage_pct=60.0, n_holdings=3, escalate=True, as_of="",
        source="amc_website",
    )
    path = _write_sidecar(
        tmp_path, {f"{AMC}|Test No Date Fund|{UNKNOWN_MONTH}": entry})
    for as_of in ("", "   ", None):
        record = lookup(AMC, "Test No Date Fund", as_of, path=path)
        assert record is not None
        assert record == entry
        assert record["tier_code"] == "T2"
    assert lookup(AMC, "Test No Date Fund", "not-a-date", path=path) == entry


# ---------------------------------------------------------------------------
# 4: display_label maps the §11.0 ladder (T0/T1/T2/T3/None)
# ---------------------------------------------------------------------------

def test_display_label_maps_the_tier_ladder():
    assert display_label(_record(tier="COMPLETE_100", tier_code="T0")) == "Full portfolio"
    assert display_label(_record()) == "Top 10 only"
    assert display_label(_record(tier="ESCALATE", tier_code="T2")) == \
        "Pending — hunting for full portfolio"
    assert display_label(_record(tier="MANUAL", tier_code="T3")) == "Pending manual"
    assert display_label(None) == "Unknown"
    assert display_label({}) == "Unknown"
    assert display_label({"nonsense": True}) == "Unknown"
    assert display_label(_record(tier="WEIRD", tier_code="T9")) == "Unknown"
    assert display_label({"tier": "COMPLETE_100"}) == "Full portfolio"
    assert display_label({"tier": "MANUAL"}) == "Pending manual"
    assert display_label({"tier_code": "T1"}) == "Top 10 only"


# ---------------------------------------------------------------------------
# is_full_portfolio: the template gate is True for a proven T0 only
# ---------------------------------------------------------------------------

def test_is_full_portfolio_gates_templates():
    assert is_full_portfolio(_record(tier="COMPLETE_100", tier_code="T0")) is True
    assert is_full_portfolio({"tier": "COMPLETE_100"}) is True
    assert is_full_portfolio(_record()) is False
    assert is_full_portfolio(_record(tier="ESCALATE", tier_code="T2")) is False
    assert is_full_portfolio(_record(tier="MANUAL", tier_code="T3")) is False
    assert is_full_portfolio(None) is False
    assert is_full_portfolio({}) is False
    assert is_full_portfolio("T0") is False


# ---------------------------------------------------------------------------
# 5: missing / malformed sidecar yields {} and None without raising
# ---------------------------------------------------------------------------

def test_missing_and_malformed_sidecars_degrade_without_raising(tmp_path):
    missing = tmp_path / "reference" / "absent.json"
    assert load_tiers(missing) == {}
    assert lookup(AMC, FUND, "2026-07-31", path=missing) is None

    broken = tmp_path / "reference" / "broken.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text('{"tier_code": "T0"', encoding="utf-8")
    assert load_tiers(broken) == {}
    assert lookup(AMC, FUND, "2026-07-31", path=broken) is None

    array = tmp_path / "reference" / "array.json"
    array.parent.mkdir(parents=True, exist_ok=True)
    array.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_tiers(array) == {}

    assert load_tiers(tmp_path) == {}

    mixed = _write_sidecar(tmp_path / "mixed", {
        "ok-key": _record(),
        "bad-value": "not-a-dict",
        "bad-list": [1, 2],
    })
    loaded = load_tiers(mixed)
    assert set(loaded) == {"ok-key"}
    assert loaded["ok-key"]["tier_code"] == "T1"
    assert lookup(AMC, FUND, "2026-07-31", path=mixed) is None


# ---------------------------------------------------------------------------
# tier_key: the writer's exact key format, incl. the unknown bucket
# ---------------------------------------------------------------------------

def test_tier_key_mirrors_the_writer_format():
    assert tier_key(AMC, FUND, "2026-07-31") == f"{AMC}|{FUND}|2026-07"
    assert tier_key(AMC, FUND, "2026-07") == f"{AMC}|{FUND}|2026-07"
    assert tier_key(AMC, FUND, "") == f"{AMC}|{FUND}|{UNKNOWN_MONTH}"
    assert tier_key(AMC, FUND, None) == f"{AMC}|{FUND}|{UNKNOWN_MONTH}"
    assert tier_key(None, None, None) == "||unknown"


# ---------------------------------------------------------------------------
# default-path wiring: DATA_REFERENCE / integrity_tiers.json, CWD-relative
# ---------------------------------------------------------------------------

def test_default_paths_point_at_the_data_reference_sidecar():
    assert DATA_REFERENCE == Path("data") / "reference"
    assert DEFAULT_TIERS_PATH == DATA_REFERENCE / "integrity_tiers.json"


def test_default_path_resolves_relative_to_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    entry = _record()
    sidecar = tmp_path / "data" / "reference" / "integrity_tiers.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({f"{AMC}|{FUND}|2026-07": entry}), encoding="utf-8")
    assert load_tiers() == {f"{AMC}|{FUND}|2026-07": entry}
    assert lookup(AMC, FUND, "2026-07-31") == entry
    assert lookup(AMC, "Absent", "2026-07-31") is None


# ---------------------------------------------------------------------------
# 6: the suite never touches the real data/reference/integrity_tiers.json
# ---------------------------------------------------------------------------

def test_suite_never_touches_the_real_sidecar(tmp_path):
    real = Path("data") / "reference" / "integrity_tiers.json"
    before = real.stat() if real.exists() else None
    path = _write_sidecar(tmp_path, {f"{AMC}|{FUND}|2026-07": _record()})
    assert load_tiers(path)
    assert lookup(AMC, FUND, "2026-07-31", path=path) is not None
    assert lookup(AMC, "Absent", "", path=path) is None
    assert display_label(None) == "Unknown"
    if before is not None:
        after = real.stat()
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
