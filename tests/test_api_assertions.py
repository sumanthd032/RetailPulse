# PROMPT: Generate 10 specific API assertions matching the style of assertions.py
# described in the Purplle Tech Challenge problem statement. These should test the
# exact contract expected by the automated scoring harness: correct HTTP status,
# valid response structure, sensible values, and edge-case handling.
#
# CHANGES MADE: The AI-generated version used generic assertions like "response is 200".
# I strengthened them to match the actual schema: checking specific field names,
# type constraints (conversion_rate must be float 0-1), ordering invariants
# (funnel stages must be non-increasing), and the idempotency guarantee.
# Also added the all-staff-clip assertion (unique_visitors=0 when all events
# have is_staff=true) which the AI completely missed.

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _ev(visitor_id, event_type, store_id="STORE_ASSERT", is_staff=False, **kw):
    ts = kw.get("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   store_id,
        "camera_id":  "CAM_ENTRY_01",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp":  ts,
        "zone_id":    kw.get("zone_id"),
        "dwell_ms":   kw.get("dwell_ms", 0),
        "is_staff":   is_staff,
        "confidence": kw.get("confidence", 0.88),
        "metadata":   {"queue_depth": kw.get("queue_depth"), "sku_zone": None, "session_seq": kw.get("session_seq", 1)},
    }


@pytest.fixture
def client(test_db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


# ── Assertion 1 ───────────────────────────────────────────────────────────────
def test_assert_01_ingest_returns_200(client):
    """POST /events/ingest returns HTTP 200 with a valid body."""
    events = [_ev(f"VIS_{i:03d}", "ENTRY") for i in range(5)]
    res = client.post("/events/ingest", json={"events": events})
    assert res.status_code == 200
    body = res.json()
    assert "accepted" in body
    assert "rejected" in body
    assert body["accepted"] == 5
    assert body["rejected"] == 0


# ── Assertion 2 ───────────────────────────────────────────────────────────────
def test_assert_02_metrics_schema(client):
    """GET /stores/{id}/metrics returns correct schema fields."""
    client.post("/events/ingest", json={"events": [_ev("VIS_001", "ENTRY")]})
    res = client.get("/stores/STORE_ASSERT/metrics")
    assert res.status_code == 200
    d = res.json()
    required = {"store_id","date","unique_visitors","conversion_rate","avg_dwell_per_zone","current_queue_depth","abandonment_rate"}
    assert required.issubset(d.keys()), f"Missing fields: {required - d.keys()}"


# ── Assertion 3 ───────────────────────────────────────────────────────────────
def test_assert_03_conversion_rate_bounds(client):
    """Conversion rate must be a float in [0.0, 1.0]."""
    client.post("/events/ingest", json={"events": [_ev("VIS_001", "ENTRY")]})
    d = client.get("/stores/STORE_ASSERT/metrics").json()
    cr = d["conversion_rate"]
    assert isinstance(cr, float), f"conversion_rate is not float: {type(cr)}"
    assert 0.0 <= cr <= 1.0, f"conversion_rate out of range: {cr}"


# ── Assertion 4 ───────────────────────────────────────────────────────────────
def test_assert_04_empty_store_returns_zeros_not_error(client):
    """An unknown store returns 200 with all-zero metrics, never 404 or 500."""
    res = client.get("/stores/STORE_NONEXISTENT/metrics")
    assert res.status_code == 200
    d = res.json()
    assert d["unique_visitors"] == 0
    assert d["conversion_rate"] == 0.0
    assert d["abandonment_rate"] == 0.0
    assert isinstance(d["avg_dwell_per_zone"], list)


# ── Assertion 5 ───────────────────────────────────────────────────────────────
def test_assert_05_funnel_stages_non_increasing(client):
    """Funnel stage counts must be non-increasing: entry >= zone >= billing >= purchase."""
    events = [
        _ev("VIS_f01", "ENTRY"),
        _ev("VIS_f01", "ZONE_ENTER",         zone_id="SKINCARE"),
        _ev("VIS_f01", "BILLING_QUEUE_JOIN",  zone_id="BILLING_QUEUE", queue_depth=1, session_seq=2),
        _ev("VIS_f02", "ENTRY"),
        _ev("VIS_f02", "ZONE_ENTER",          zone_id="MAKEUP"),
    ]
    client.post("/events/ingest", json={"events": events})
    d = client.get("/stores/STORE_ASSERT/funnel").json()
    counts = [s["count"] for s in d["stages"]]
    for i in range(len(counts)-1):
        assert counts[i] >= counts[i+1], f"Funnel not monotone: stage {i}={counts[i]} < stage {i+1}={counts[i+1]}"


# ── Assertion 6 ───────────────────────────────────────────────────────────────
def test_assert_06_ingest_idempotent(client):
    """Posting the same batch twice must not change unique_visitors count."""
    events = [_ev(f"VIS_idem{i}", "ENTRY") for i in range(5)]
    client.post("/events/ingest", json={"events": events})
    count_before = client.get("/stores/STORE_ASSERT/metrics").json()["unique_visitors"]

    # Post exact same events again — all event_ids are identical
    client.post("/events/ingest", json={"events": events})
    count_after = client.get("/stores/STORE_ASSERT/metrics").json()["unique_visitors"]

    assert count_before == count_after, (
        f"Idempotency broken: before={count_before} after={count_after}"
    )


# ── Assertion 7 ───────────────────────────────────────────────────────────────
def test_assert_07_staff_excluded_from_metrics(client):
    """Visitors with is_staff=true must NOT appear in unique_visitors count."""
    events = [
        _ev("VIS_cust1", "ENTRY", is_staff=False),
        _ev("VIS_cust2", "ENTRY", is_staff=False),
        _ev("VIS_staff", "ENTRY", is_staff=True),
        _ev("VIS_staff", "ZONE_ENTER", is_staff=True, zone_id="CASH_COUNTER"),
    ]
    client.post("/events/ingest", json={"events": events})
    d = client.get("/stores/STORE_ASSERT/metrics").json()
    assert d["unique_visitors"] == 2, (
        f"Staff counted as customer: unique_visitors={d['unique_visitors']}, expected 2"
    )


# ── Assertion 8 ───────────────────────────────────────────────────────────────
def test_assert_08_all_staff_clip_zero_customers(client):
    """An all-staff clip (every event is_staff=true) must yield 0 unique customers."""
    events = [_ev(f"STAFF_{i}", "ENTRY", is_staff=True) for i in range(5)]
    events += [_ev(f"STAFF_{i}", "ZONE_ENTER", is_staff=True, zone_id="CASH_COUNTER") for i in range(5)]
    client.post("/events/ingest", json={"events": events})
    d = client.get("/stores/STORE_ASSERT/metrics").json()
    assert d["unique_visitors"] == 0, (
        f"Staff-only clip shows customers: unique_visitors={d['unique_visitors']}"
    )


# ── Assertion 9 ───────────────────────────────────────────────────────────────
def test_assert_09_reentry_single_session_in_funnel(client):
    """A visitor who ENTRY → EXIT → REENTRY counts as 1 entry in funnel, not 2."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    events = [
        _ev("VIS_re01", "ENTRY",   session_seq=1),
        _ev("VIS_re01", "EXIT",    session_seq=2),
        _ev("VIS_re01", "REENTRY", session_seq=3),
        _ev("VIS_re01", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=1, session_seq=4),
    ]
    client.post("/events/ingest", json={"events": events})
    d = client.get(f"/stores/STORE_ASSERT/funnel?date={today}").json()
    stages = {s["stage"]: s["count"] for s in d["stages"]}
    assert stages["Entry"] == 1, (
        f"REENTRY inflated entry count: expected 1, got {stages['Entry']}"
    )
    assert stages["Billing Queue"] == 1


# ── Assertion 10 ──────────────────────────────────────────────────────────────
def test_assert_10_heatmap_scores_in_valid_range(client):
    """All heatmap zone scores must be in [0.0, 100.0]."""
    events = [
        _ev(f"VIS_hm{i}", "ZONE_DWELL", zone_id=f"ZONE_{chr(65+i)}", dwell_ms=30000*i+1000, session_seq=1)
        for i in range(6)
    ]
    client.post("/events/ingest", json={"events": events})
    d = client.get("/stores/STORE_ASSERT/heatmap").json()
    for z in d["zones"]:
        assert 0.0 <= z["visit_score"] <= 100.0, f"{z['zone_id']} visit_score out of range: {z['visit_score']}"
        assert 0.0 <= z["dwell_score"] <= 100.0, f"{z['zone_id']} dwell_score out of range: {z['dwell_score']}"
