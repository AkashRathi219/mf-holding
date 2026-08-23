"""ANA4: proposal output carries performance tables + locked disclaimer +
timestamped methodology-version stamp."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from webapp.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    from conftest import ensure_token
    return ensure_token(client, {"name": "Prop", "email": "prop@test.local",
                                 "org": "", "password": "password123"})


def test_proposal_has_perf_section_stamp_and_locked_disclaimer(client, auth):
    # scheme id 1 exists in the sandbox fixture DB
    r = client.post("/api/proposal", headers=auth,
                    json={"items": [{"type": "scheme", "id": 1, "weight": 100.0}]})
    assert r.status_code == 200, r.text
    md = r.json()["markdown"]
    assert "## 3. Performance snapshot" in md
    assert "| Scheme | Alloc % | 1Y CAGR | 3Y CAGR | 5Y CAGR |" in md
    assert "insufficient NAV history" in md          # honest gap, sandbox has no history
    import re
    assert re.search(r"Methodology stamp: `perf-v\d+", md)
    assert re.search(r"generated \d{4}-\d{2}-\d{2}T\d{2}:", md)  # timestamped
    assert "## 4. Key Rationale" in md               # sections survived renumbering
    assert "**Disclaimer (locked):**" in md
    assert "Past performance is not indicative of future returns." in md
