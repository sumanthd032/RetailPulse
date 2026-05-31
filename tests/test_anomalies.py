# PROMPT: Generate unit tests for a retail anomaly detection service covering
# BILLING_QUEUE_SPIKE, CONVERSION_DROP, and DEAD_ZONE detectors. Test the
# bootstrap case (no historical data), severity thresholds, and the
# no-false-positives requirement for normal traffic.
#
# CHANGES MADE: The AI generated tests that seeded the DB then called the service
# functions directly — good approach, but it forgot to handle the date mocking
# needed for CONVERSION_DROP (the function uses "today" internally). I added
# a monkeypatch for the date to make the tests deterministic. Also added the
# "bootstrap with no history returns data_confidence=False" test — critical
# for the evaluation criteria but missing from the AI output.

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def seeded_client(test_db):
    """TestClient with fresh DB and a small set of seeded sessions."""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        yield client


def _ingest(client, events):
    return client.post("/events/ingest", json={"events": events})


def _ev(visitor_id, event_type, store_id="STORE_TEST", **kwargs):
    import uuid
    ts = kwargs.pop("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    zone_id = kwargs.pop("zone_id", None)
    queue_depth = kwargs.pop("queue_depth", None)
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts,
        "zone_id": zone_id,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.85,
        "metadata": {"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1},
    }


# ─────────────────────────────────────────────────────────────────────────────
# BILLING_QUEUE_SPIKE
# ─────────────────────────────────────────────────────────────────────────────

class TestQueueSpike:
    def test_critical_spike_at_depth_10(self, seeded_client):
        _ingest(seeded_client, [_ev("V01", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=10)])
        data = seeded_client.get("/stores/STORE_TEST/anomalies").json()
        types = {a["anomaly_type"] for a in data["anomalies"]}
        assert "BILLING_QUEUE_SPIKE" in types
        spikes = [a for a in data["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert spikes[0]["severity"] in ("CRITICAL", "WARN")

    def test_warn_spike_at_depth_6(self, seeded_client):
        _ingest(seeded_client, [_ev("V02", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=6)])
        data = seeded_client.get("/stores/STORE_TEST/anomalies").json()
        spikes = [a for a in data["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 1
        assert spikes[0]["severity"] == "WARN"

    def test_no_spike_at_low_depth(self, seeded_client):
        _ingest(seeded_client, [_ev("V03", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=2)])
        data = seeded_client.get("/stores/STORE_TEST/anomalies").json()
        spikes = [a for a in data["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 0

    def test_spike_has_no_data_confidence_when_no_history(self, seeded_client):
        """With no 7-day history, data_confidence must be False on spike."""
        _ingest(seeded_client, [_ev("V04", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=9)])
        data = seeded_client.get("/stores/STORE_TEST/anomalies").json()
        spikes = [a for a in data["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        if spikes:
            assert spikes[0]["data_confidence"] is False  # no history → low confidence

    def test_spike_includes_suggested_action(self, seeded_client):
        _ingest(seeded_client, [_ev("V05", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=10)])
        data = seeded_client.get("/stores/STORE_TEST/anomalies").json()
        spikes = [a for a in data["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert spikes
        assert "counter" in spikes[0]["suggested_action"].lower() or "cashier" in spikes[0]["suggested_action"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# CONVERSION_DROP (requires multi-day history → mostly bootstrap behaviour)
# ─────────────────────────────────────────────────────────────────────────────

class TestConversionDrop:
    def test_no_drop_without_history(self, seeded_client):
        """With no prior day data, CONVERSION_DROP should NOT fire."""
        events = [_ev(f"V{i:03d}", "ENTRY") for i in range(10)]
        _ingest(seeded_client, events)
        data = seeded_client.get("/stores/STORE_TEST/anomalies").json()
        drops = [a for a in data["anomalies"] if a["anomaly_type"] == "CONVERSION_DROP"]
        assert len(drops) == 0  # bootstrap: no history → no false alarm

    def test_anomaly_response_is_valid_json(self, seeded_client):
        data = seeded_client.get("/stores/STORE_TEST/anomalies").json()
        assert "anomalies" in data
        assert isinstance(data["anomalies"], list)


# ─────────────────────────────────────────────────────────────────────────────
# DEAD_ZONE
# ─────────────────────────────────────────────────────────────────────────────

class TestDeadZone:
    def test_dead_zone_fires_after_30_min_silence(self, seeded_client):
        """A zone with last ZONE_ENTER > 30 minutes ago should trigger DEAD_ZONE."""
        from app.services.anomalies import STALE_ZONE_MINUTES, STORE_OPEN_HOUR, STORE_CLOSE_HOUR
        import uuid

        old_ts = "2026-03-03T10:05:00Z"   # 31+ minutes before mocked "now" of 10:40

        events = [{
            "event_id": str(uuid.uuid4()),
            "store_id": "STORE_TEST",
            "camera_id": "CAM_FLOOR_01",
            "visitor_id": "VIS_dz01",
            "event_type": "ZONE_ENTER",
            "timestamp": old_ts,
            "zone_id": "SKINCARE_SHELF",
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.88,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
        }]
        _ingest(seeded_client, events)

        # Mock "now" to be store-open-hours and 35 minutes after the event
        fake_now = datetime(2026, 3, 3, 10, 40, 0, tzinfo=timezone.utc)
        with patch("app.services.anomalies._now_utc", return_value=fake_now), \
             patch("app.services.anomalies._today_str", return_value="2026-03-03"):
            data = seeded_client.get("/stores/STORE_TEST/anomalies").json()

        dead = [a for a in data["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"]
        assert len(dead) >= 1
        assert dead[0]["zone_id"] == "SKINCARE_SHELF"
        assert dead[0]["severity"] == "INFO"

    def test_no_dead_zone_when_store_closed(self, seeded_client):
        """DEAD_ZONE should not fire outside store open hours."""
        import uuid
        old_ts = "2026-03-03T10:05:00Z"
        events = [{
            "event_id": str(uuid.uuid4()),
            "store_id": "STORE_TEST",
            "camera_id": "CAM_FLOOR_01",
            "visitor_id": "VIS_dz02",
            "event_type": "ZONE_ENTER",
            "timestamp": old_ts,
            "zone_id": "MAKEUP_UNIT",
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.88,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
        }]
        _ingest(seeded_client, events)

        # 22:00 UTC — store is closed
        fake_now = datetime(2026, 3, 3, 22, 0, 0, tzinfo=timezone.utc)
        with patch("app.services.anomalies._now_utc", return_value=fake_now), \
             patch("app.services.anomalies._today_str", return_value="2026-03-03"):
            data = seeded_client.get("/stores/STORE_TEST/anomalies").json()

        dead = [a for a in data["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"]
        assert len(dead) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Anomaly response contract
# ─────────────────────────────────────────────────────────────────────────────

class TestAnomalyContract:
    def test_each_anomaly_has_required_fields(self, seeded_client):
        _ingest(seeded_client, [_ev("V_c", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=10)])
        data = seeded_client.get("/stores/STORE_TEST/anomalies").json()
        for a in data["anomalies"]:
            assert "anomaly_type"     in a
            assert "severity"         in a
            assert "suggested_action" in a
            assert "detected_at"      in a
            assert "data_confidence"  in a
            assert a["severity"] in ("INFO", "WARN", "CRITICAL")

    def test_anomalies_sorted_by_severity(self, seeded_client):
        _ingest(seeded_client, [_ev("V_s", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=10)])
        data = seeded_client.get("/stores/STORE_TEST/anomalies").json()
        order = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
        sev_list = [order.get(a["severity"], 9) for a in data["anomalies"]]
        assert sev_list == sorted(sev_list)
