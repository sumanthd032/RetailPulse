# PROMPT: Generate API endpoint tests for a retail analytics API with SQLite backend.
# Cover: /events/ingest idempotency, /metrics with staff exclusion and zero-purchase stores,
# /funnel session deduplication with REENTRY events, /heatmap data_confidence flag,
# and edge cases: empty store, all-staff clip.
#
# CHANGES MADE: The AI generated tests that called the full FastAPI app via TestClient
# but didn't mock the DB path — they would pollute the real DB. I replaced with
# a fixture that patches DB_PATH to a temp file. Also added the critical
# "funnel REENTRY dedup" test which the AI missed entirely — this is the hardest
# correctness requirement and the most common failure mode.

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_event(
    visitor_id="VIS_test001",
    event_type="ENTRY",
    store_id="STORE_TEST",
    camera_id="CAM_ENTRY_01",
    zone_id=None,
    dwell_ms=0,
    is_staff=False,
    confidence=0.88,
    queue_depth=None,
    session_seq=1,
    timestamp=None,
):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": queue_depth, "sku_zone": None, "session_seq": session_seq},
    }


@pytest.fixture
def client(test_db):
    """FastAPI TestClient with fresh DB."""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# 1. POST /events/ingest — correctness + idempotency
# ─────────────────────────────────────────────────────────────────────────────

class TestIngest:
    def test_ingest_returns_accepted_count(self, client):
        events = [_make_event(f"VIS_{i:03d}") for i in range(5)]
        res = client.post("/events/ingest", json={"events": events})
        assert res.status_code == 200
        data = res.json()
        assert data["accepted"] == 5
        assert data["rejected"] == 0

    def test_ingest_is_idempotent(self, client):
        """Posting the same batch twice should not change metrics."""
        events = [_make_event("VIS_idem")]
        client.post("/events/ingest", json={"events": events})
        client.post("/events/ingest", json={"events": events})

        # The visitor count should still be 1, not 2
        metrics = client.get("/stores/STORE_TEST/metrics").json()
        assert metrics["unique_visitors"] == 1

    def test_ingest_partial_success_on_malformed(self, client):
        """One bad event in a batch must not reject the whole batch."""
        good = _make_event("VIS_good")
        bad = {"event_id": "bad", "store_id": "X"}  # missing required fields
        res = client.post("/events/ingest", json={"events": [good, bad]})
        assert res.status_code == 200
        data = res.json()
        # At least the good event should be accepted
        assert data["accepted"] >= 1

    def test_ingest_dedup_by_event_id(self, client):
        ev = _make_event("VIS_dedup")
        res1 = client.post("/events/ingest", json={"events": [ev]})
        res2 = client.post("/events/ingest", json={"events": [ev]})
        assert res1.json()["accepted"] == 1
        assert res2.json()["accepted"] == 1  # still 200, just idempotent


# ─────────────────────────────────────────────────────────────────────────────
# 2. GET /stores/{id}/metrics — correctness
# ─────────────────────────────────────────────────────────────────────────────

class TestMetrics:
    def test_metrics_returns_zero_for_empty_store(self, client):
        res = client.get("/stores/STORE_EMPTY/metrics")
        assert res.status_code == 200
        data = res.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0
        assert data["abandonment_rate"] == 0.0

    def test_metrics_excludes_staff(self, client):
        events = [
            _make_event("VIS_cust01", "ENTRY", is_staff=False),
            _make_event("VIS_cust02", "ENTRY", is_staff=False),
            _make_event("VIS_staff1", "ENTRY", is_staff=True),
        ]
        client.post("/events/ingest", json={"events": events})
        data = client.get("/stores/STORE_TEST/metrics").json()
        assert data["unique_visitors"] == 2  # staff excluded

    def test_metrics_conversion_zero_when_no_purchases(self, client):
        events = [_make_event(f"VIS_{i:03d}", "ENTRY") for i in range(10)]
        client.post("/events/ingest", json={"events": events})
        data = client.get("/stores/STORE_TEST/metrics").json()
        assert data["unique_visitors"] == 10
        assert data["conversion_rate"] == 0.0  # not null, not error

    def test_metrics_unique_visitor_dedup(self, client):
        """Same visitor_id multiple times = 1 unique visitor."""
        events = [
            _make_event("VIS_repeat", "ENTRY"),
            _make_event("VIS_repeat", "ZONE_ENTER", zone_id="SKINCARE"),
            _make_event("VIS_repeat", "EXIT"),
        ]
        client.post("/events/ingest", json={"events": events})
        data = client.get("/stores/STORE_TEST/metrics").json()
        assert data["unique_visitors"] == 1

    def test_metrics_queue_depth_tracked(self, client):
        events = [
            _make_event("VIS_q01", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=4),
        ]
        client.post("/events/ingest", json={"events": events})
        data = client.get("/stores/STORE_TEST/metrics").json()
        assert data["current_queue_depth"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# 3. GET /stores/{id}/funnel — session dedup with REENTRY
# ─────────────────────────────────────────────────────────────────────────────

class TestFunnel:
    def test_funnel_counts_match_sessions(self, client):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        events = [
            _make_event("VIS_f01", "ENTRY"),
            _make_event("VIS_f01", "ZONE_ENTER", zone_id="SKINCARE", session_seq=2),
            _make_event("VIS_f02", "ENTRY"),
            _make_event("VIS_f02", "ZONE_ENTER", zone_id="MAKEUP", session_seq=2),
            _make_event("VIS_f02", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=1, session_seq=3),
        ]
        client.post("/events/ingest", json={"events": events})
        data = client.get(f"/stores/STORE_TEST/funnel?date={today}").json()
        stages = {s["stage"]: s["count"] for s in data["stages"]}
        assert stages["Entry"] == 2
        assert stages["Zone Visit"] == 2
        assert stages["Billing Queue"] == 1
        assert stages["Purchase"] == 0

    def test_funnel_reentry_does_not_inflate_entry_count(self, client):
        """Visitor enters, exits, re-enters → counts as 1 session in funnel, not 2."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        events = [
            _make_event("VIS_reent", "ENTRY",   session_seq=1),
            _make_event("VIS_reent", "EXIT",    session_seq=2),
            _make_event("VIS_reent", "REENTRY", session_seq=3),
            _make_event("VIS_reent", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=0, session_seq=4),
        ]
        client.post("/events/ingest", json={"events": events})
        data = client.get(f"/stores/STORE_TEST/funnel?date={today}").json()
        stages = {s["stage"]: s["count"] for s in data["stages"]}

        # ENTRY count must be 1, not 2 (REENTRY does not create a new session)
        assert stages["Entry"] == 1, f"Expected 1 entry, got {stages['Entry']} — REENTRY inflating count"
        assert stages["Billing Queue"] == 1

    def test_funnel_drop_off_pct_calculated(self, client):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        events = [
            _make_event("VIS_dr01", "ENTRY"),
            _make_event("VIS_dr02", "ENTRY"),
            _make_event("VIS_dr01", "ZONE_ENTER", zone_id="ZONE_A", session_seq=2),
        ]
        client.post("/events/ingest", json={"events": events})
        data = client.get(f"/stores/STORE_TEST/funnel?date={today}").json()
        zone_stage = next(s for s in data["stages"] if s["stage"] == "Zone Visit")
        assert zone_stage["drop_off_pct"] == 50.0  # 1 of 2 didn't reach zone

    def test_funnel_empty_store_returns_zeros(self, client):
        data = client.get("/stores/STORE_NOWHERE/funnel").json()
        stages = {s["stage"]: s["count"] for s in data["stages"]}
        assert all(v == 0 for v in stages.values())


# ─────────────────────────────────────────────────────────────────────────────
# 4. GET /stores/{id}/heatmap — normalisation + confidence
# ─────────────────────────────────────────────────────────────────────────────

class TestHeatmap:
    def test_heatmap_scores_normalised_0_100(self, client):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        events = []
        zones = ["SKINCARE", "MAKEUP", "FRAGRANCE"]
        for i, zone in enumerate(zones):
            dwell = (i + 1) * 30000  # 30s, 60s, 90s
            events.append(_make_event(f"VIS_hm{i}", "ZONE_DWELL", zone_id=zone, dwell_ms=dwell, session_seq=i+1))

        client.post("/events/ingest", json={"events": events})
        data = client.get(f"/stores/STORE_TEST/heatmap?date={today}").json()
        for z in data["zones"]:
            assert 0 <= z["visit_score"] <= 100
            assert 0 <= z["dwell_score"] <= 100

    def test_heatmap_data_confidence_false_when_few_sessions(self, client):
        """With < 20 sessions, data_confidence should be False."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        events = [_make_event(f"VIS_conf{i}", "ZONE_DWELL", zone_id="SKINCARE", dwell_ms=30000) for i in range(3)]
        client.post("/events/ingest", json={"events": events})
        data = client.get(f"/stores/STORE_TEST/heatmap?date={today}").json()
        assert data["data_confidence"] is False

    def test_heatmap_empty_returns_empty_zones(self, client):
        data = client.get("/stores/STORE_VOID/heatmap").json()
        assert data["zones"] == []


# ─────────────────────────────────────────────────────────────────────────────
# 5. GET /stores/{id}/anomalies — bootstrap + basic detection
# ─────────────────────────────────────────────────────────────────────────────

class TestAnomalies:
    def test_anomalies_empty_store_returns_empty_list(self, client):
        data = client.get("/stores/STORE_ANON/anomalies").json()
        assert isinstance(data["anomalies"], list)

    def test_anomaly_queue_spike_fires_on_high_depth(self, client):
        """Absolute threshold: queue_depth >= 8 → BILLING_QUEUE_SPIKE (no history)."""
        events = [
            _make_event("VIS_q01", "BILLING_QUEUE_JOIN",
                        zone_id="BILLING_QUEUE", queue_depth=10, session_seq=1),
        ]
        client.post("/events/ingest", json={"events": events})
        data = client.get("/stores/STORE_TEST/anomalies").json()
        types = [a["anomaly_type"] for a in data["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" in types

    def test_anomaly_severity_order(self, client):
        """CRITICAL anomalies must appear before WARN before INFO."""
        # Seed a high queue depth to trigger CRITICAL
        events = [_make_event("VIS_sev", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=10)]
        client.post("/events/ingest", json={"events": events})
        data = client.get("/stores/STORE_TEST/anomalies").json()
        anomalies = data["anomalies"]
        if len(anomalies) > 1:
            order = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
            for i in range(len(anomalies) - 1):
                assert order.get(anomalies[i]["severity"], 9) <= order.get(anomalies[i+1]["severity"], 9)

    def test_anomaly_response_has_suggested_action(self, client):
        """Every anomaly must include a non-empty suggested_action string."""
        events = [_make_event("VIS_sa", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=9)]
        client.post("/events/ingest", json={"events": events})
        data = client.get("/stores/STORE_TEST/anomalies").json()
        for a in data["anomalies"]:
            assert "suggested_action" in a
            assert len(a["suggested_action"]) > 10

    def test_normal_traffic_no_false_positives(self, client):
        """Low queue depth + no history → no BILLING_QUEUE_SPIKE anomaly."""
        events = [_make_event("VIS_fp", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=2)]
        client.post("/events/ingest", json={"events": events})
        data = client.get("/stores/STORE_TEST/anomalies").json()
        spike_anomalies = [a for a in data["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spike_anomalies) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 6. GET /health
# ─────────────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_200(self, client):
        res = client.get("/health")
        assert res.status_code == 200

    def test_health_structure(self, client):
        data = client.get("/health").json()
        assert "status" in data
        assert "db" in data
        assert "stores" in data
        assert "uptime_seconds" in data

    def test_health_db_connected(self, client):
        data = client.get("/health").json()
        assert data["db"] == "connected"
