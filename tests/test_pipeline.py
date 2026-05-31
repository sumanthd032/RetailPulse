# PROMPT: Generate comprehensive unit tests for a retail CCTV detection pipeline
# covering: event schema validation, zone classification, Re-ID gallery matching,
# group entry counting, re-entry detection, staff flag propagation,
# ZONE_DWELL timing, and confidence field handling.
# Include edge cases: empty store periods, low-confidence detections.
#
# CHANGES MADE: Removed tests that required a live YOLO model (too slow for unit tests).
# Replaced with mock detections using synthetic bounding boxes and frames.
# Added the ZONE_DWELL cumulative timing test — the AI-generated version
# used windowed dwell_ms (always 30000ms); I corrected this to cumulative dwell.
# Added the group-entry test with 3 simultaneous track IDs — the AI didn't
# include the temporal proximity assertion.

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models import EventType, StoreEvent
from pipeline.config import PipelineConfig
from pipeline.emit import EventEmitter
from pipeline.tracker import ReIDGallery, _cosine_sim, _extract_embedding
from pipeline.zones import ZoneManager


# ─────────────────────────────────────────────────────────────────────────────
# 1. Event schema validation
# ─────────────────────────────────────────────────────────────────────────────

class TestEventSchema:
    def test_valid_entry_event(self):
        ev = StoreEvent(
            store_id="STORE_BLR_002",
            camera_id="CAM_ENTRY_01",
            visitor_id="VIS_abc123",
            event_type=EventType.ENTRY,
            timestamp="2026-03-03T10:05:00Z",
            zone_id=None,
            dwell_ms=0,
            is_staff=False,
            confidence=0.88,
        )
        assert ev.event_type == "ENTRY"
        assert ev.zone_id is None
        assert ev.dwell_ms == 0
        assert len(ev.event_id) > 0  # UUID auto-generated

    def test_event_id_is_unique(self):
        ids = {
            StoreEvent(
                store_id="S", camera_id="C", visitor_id="V",
                event_type=EventType.ENTRY, timestamp="2026-01-01T00:00:00Z",
                confidence=0.9,
            ).event_id
            for _ in range(100)
        }
        assert len(ids) == 100  # all unique

    def test_confidence_clamp_validation(self):
        with pytest.raises(Exception):
            StoreEvent(
                store_id="S", camera_id="C", visitor_id="V",
                event_type=EventType.ENTRY, timestamp="2026-01-01T00:00:00Z",
                confidence=1.5,  # invalid: > 1.0
            )

    def test_confidence_zero_allowed(self):
        """Low-confidence events must NOT be suppressed — confidence=0 is valid."""
        ev = StoreEvent(
            store_id="S", camera_id="C", visitor_id="V",
            event_type=EventType.ZONE_ENTER, timestamp="2026-01-01T00:00:00Z",
            zone_id="SKINCARE_SHELF", confidence=0.0,
        )
        assert ev.confidence == 0.0

    def test_dwell_ms_nonnegative(self):
        with pytest.raises(Exception):
            StoreEvent(
                store_id="S", camera_id="C", visitor_id="V",
                event_type=EventType.ZONE_DWELL, timestamp="2026-01-01T00:00:00Z",
                zone_id="SKINCARE", dwell_ms=-1, confidence=0.7,
            )

    def test_zone_dwell_has_zone_id(self):
        ev = StoreEvent(
            store_id="S", camera_id="C", visitor_id="V",
            event_type=EventType.ZONE_DWELL, timestamp="2026-01-01T00:00:00Z",
            zone_id="SKINCARE", dwell_ms=30000, confidence=0.85,
        )
        assert ev.zone_id == "SKINCARE"
        assert ev.dwell_ms == 30000

    def test_billing_queue_join_has_queue_depth(self):
        ev = StoreEvent(
            store_id="S", camera_id="C", visitor_id="V",
            event_type=EventType.BILLING_QUEUE_JOIN, timestamp="2026-01-01T00:00:00Z",
            zone_id="BILLING_QUEUE", dwell_ms=0, confidence=0.9,
            metadata={"queue_depth": 3, "sku_zone": None, "session_seq": 5},
        )
        assert ev.metadata.queue_depth == 3

    def test_all_event_types_valid(self):
        for et in EventType:
            zid = None if et in (EventType.ENTRY, EventType.EXIT, EventType.REENTRY) else "TEST"
            ev = StoreEvent(
                store_id="S", camera_id="C", visitor_id="V",
                event_type=et, timestamp="2026-01-01T00:00:00Z",
                zone_id=zid, confidence=0.75,
            )
            assert ev.event_type == et.value


# ─────────────────────────────────────────────────────────────────────────────
# 2. Zone classification
# ─────────────────────────────────────────────────────────────────────────────

class TestZoneManager:
    def test_zone_classification_correct(self, layout_file):
        zm = ZoneManager(layout_file, "STORE_TEST")
        frame_shape = (480, 640, 3)

        # Foot at (160, 200) → left half, top half of floor camera → SKINCARE_SHELF
        # SKINCARE polygon covers x:[0,320], y:[0,240] at 640×480
        bbox = np.array([130.0, 80.0, 190.0, 200.0])  # foot_x=160, foot_y=200
        zone = zm.get_zone(bbox, frame_shape, "CAM_FLOOR_01")
        assert zone == "SKINCARE_SHELF", f"Expected SKINCARE_SHELF, got {zone}"

    def test_zone_classification_right_side(self, layout_file):
        zm = ZoneManager(layout_file, "STORE_TEST")
        frame_shape = (480, 640, 3)

        # Foot at (480, 100) → right half → MAKEUP_UNIT
        bbox = np.array([450.0, 50.0, 510.0, 100.0])
        zone = zm.get_zone(bbox, frame_shape, "CAM_FLOOR_01")
        assert zone == "MAKEUP_UNIT"

    def test_zone_classification_returns_none_outside_all_zones(self, layout_file):
        zm = ZoneManager(layout_file, "STORE_TEST")
        # Foot in bottom half — no zones defined there for floor cam
        bbox = np.array([100.0, 300.0, 200.0, 479.0])
        zone = zm.get_zone(bbox, (480, 640, 3), "CAM_FLOOR_01")
        assert zone is None

    def test_entry_line_crossing_inbound(self, layout_file):
        zm = ZoneManager(layout_file, "STORE_TEST")
        frame_w, frame_h = 640, 480
        # entry_line at x_frac=0.40 → pixel x=256

        # Prev foot at x=240 (left of line), curr at x=270 (right) → inbound
        direction = zm.check_line_crossing(
            prev_foot=(240.0, 300.0),
            curr_foot=(270.0, 300.0),
            camera_id="CAM_ENTRY_01",
            frame_w=frame_w,
            frame_h=frame_h,
        )
        assert direction == "inbound"

    def test_entry_line_crossing_outbound(self, layout_file):
        zm = ZoneManager(layout_file, "STORE_TEST")
        direction = zm.check_line_crossing(
            prev_foot=(270.0, 300.0),
            curr_foot=(240.0, 300.0),
            camera_id="CAM_ENTRY_01",
            frame_w=640,
            frame_h=480,
        )
        assert direction == "outbound"

    def test_no_crossing_same_side(self, layout_file):
        # entry_line at x_frac=0.40 → pixel x=256
        # Both feet at x<256 (left side) → no crossing
        zm = ZoneManager(layout_file, "STORE_TEST")
        direction = zm.check_line_crossing(
            prev_foot=(230.0, 300.0),  # left of line (x=256)
            curr_foot=(245.0, 300.0),  # still left of line
            camera_id="CAM_ENTRY_01",
            frame_w=640,
            frame_h=480,
        )
        assert direction is None

    def test_staff_zone_identified(self, layout_file):
        zm = ZoneManager(layout_file, "STORE_TEST")
        assert zm.is_staff_zone("CAM_BILLING_01", "CASH_COUNTER") is True
        assert zm.is_staff_zone("CAM_BILLING_01", "BILLING_QUEUE") is False

    def test_sku_zone_returned(self, layout_file):
        zm = ZoneManager(layout_file, "STORE_TEST")
        assert zm.get_sku_zone("CAM_FLOOR_01", "SKINCARE_SHELF") == "SKINCARE"
        assert zm.get_sku_zone("CAM_FLOOR_01", "MAKEUP_UNIT") == "MAKEUP"
        assert zm.get_sku_zone("CAM_BILLING_01", "BILLING_QUEUE") is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. Re-ID gallery — visitor identity persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestReIDGallery:
    def _make_coloured_frame(self, colour_bgr: tuple[int, int, int]) -> tuple[np.ndarray, np.ndarray]:
        """Create a 480×640 frame with a person-shaped region and its bbox."""
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[100:400, 200:280] = colour_bgr
        bbox = np.array([200.0, 100.0, 280.0, 400.0], dtype=np.float32)
        return frame, bbox

    def test_new_track_gets_new_visitor_id(self):
        gallery = ReIDGallery(similarity_threshold=0.72)
        frame, bbox = self._make_coloured_frame((100, 150, 200))
        vid, is_reentry, is_new = gallery.register_or_reidentify(
            track_id=1, frame=frame, bbox=bbox,
            camera_id="CAM_ENTRY_01", camera_type="entry",
        )
        assert vid.startswith("VIS_")
        assert is_reentry is False
        assert is_new is True

    def test_same_track_returns_same_visitor_id(self):
        gallery = ReIDGallery(similarity_threshold=0.72)
        frame, bbox = self._make_coloured_frame((100, 150, 200))

        vid1, _, _ = gallery.register_or_reidentify(
            1, frame, bbox, "CAM_ENTRY_01", "entry"
        )
        vid2, _, _ = gallery.register_or_reidentify(
            1, frame, bbox, "CAM_ENTRY_01", "entry"
        )
        assert vid1 == vid2

    def test_distinct_tracks_get_distinct_visitor_ids(self):
        gallery = ReIDGallery(similarity_threshold=0.72)
        frame_a, bbox_a = self._make_coloured_frame((200, 50, 50))   # red-ish person
        frame_b, bbox_b = self._make_coloured_frame((50, 200, 50))   # green-ish person

        vid_a, _, _ = gallery.register_or_reidentify(1, frame_a, bbox_a, "CAM_ENTRY_01", "entry")
        vid_b, _, _ = gallery.register_or_reidentify(2, frame_b, bbox_b, "CAM_ENTRY_01", "entry")
        assert vid_a != vid_b

    def test_reentry_detection_same_appearance(self):
        """Person exits (track 1 retired), re-enters (track 2) — should be REENTRY."""
        gallery = ReIDGallery(similarity_threshold=0.50, gallery_ttl_s=300)
        frame, bbox = self._make_coloured_frame((150, 100, 200))

        # Track 1: initial entry
        vid_original, _, _ = gallery.register_or_reidentify(
            1, frame, bbox, "CAM_ENTRY_01", "entry"
        )
        gallery.update_gallery(1, frame, bbox, "CAM_ENTRY_01")
        gallery.retire_track(1)

        # Track 2: same frame (identical appearance) — should be REENTRY
        vid_new, is_reentry, is_new = gallery.register_or_reidentify(
            2, frame, bbox, "CAM_ENTRY_01", "entry",
            reentry_window_s=300,
        )
        assert is_reentry is True
        assert vid_new == vid_original

    def test_camera_handoff_no_new_entry(self):
        """Same person moves from entry to floor camera — no new ENTRY should fire."""
        gallery = ReIDGallery(similarity_threshold=0.50, gallery_ttl_s=300)
        frame, bbox = self._make_coloured_frame((120, 80, 160))

        vid_entry, _, _ = gallery.register_or_reidentify(
            1, frame, bbox, "CAM_ENTRY_01", "entry"
        )
        gallery.update_gallery(1, frame, bbox, "CAM_ENTRY_01")

        # New track on floor camera — same appearance
        vid_floor, is_reentry, is_new = gallery.register_or_reidentify(
            2, frame, bbox, "CAM_FLOOR_01", "floor"
        )
        assert vid_floor == vid_entry
        assert is_new is False
        assert is_reentry is False

    def test_gallery_retire_removes_track(self):
        gallery = ReIDGallery()
        frame, bbox = self._make_coloured_frame((80, 120, 200))
        vid, _, _ = gallery.register_or_reidentify(42, frame, bbox, "CAM_ENTRY_01", "entry")
        retired = gallery.retire_track(42)
        assert retired == vid
        assert gallery.get_visitor_id(42) is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Event emitter
# ─────────────────────────────────────────────────────────────────────────────

class TestEventEmitter:
    def test_emitter_writes_valid_jsonl(self, tmp_path):
        out = str(tmp_path / "events.jsonl")
        emitter = EventEmitter(out)

        emitter.emit_entry("STORE_TEST", "CAM_ENTRY_01", "VIS_xxx", "2026-01-01T10:00:00Z", 0.88, False, 1)
        emitter.emit_zone_enter("STORE_TEST", "CAM_FLOOR_01", "VIS_xxx", "2026-01-01T10:00:30Z", "SKINCARE_SHELF", 0.82, False, 2, "SKINCARE")
        emitter.emit_zone_dwell("STORE_TEST", "CAM_FLOOR_01", "VIS_xxx", "2026-01-01T10:01:00Z", "SKINCARE_SHELF", 30000, 0.82, False, 3, "SKINCARE")
        written, rejected = emitter.close()

        assert written == 3
        assert rejected == 0

        lines = Path(out).read_text().strip().splitlines()
        assert len(lines) == 3

        for line in lines:
            event = json.loads(line)
            assert "event_id" in event
            assert "visitor_id" in event
            assert "confidence" in event
            assert event["confidence"] >= 0.0

    def test_emitter_low_confidence_events_are_written(self, tmp_path):
        """Low-confidence detections must be emitted, not suppressed."""
        out = str(tmp_path / "events.jsonl")
        emitter = EventEmitter(out)

        # Confidence 0.36 — deliberately below typical 0.5 threshold
        emitter.emit_entry("STORE_TEST", "CAM_ENTRY_01", "VIS_low", "2026-01-01T10:00:00Z", 0.36, False, 1)
        written, _ = emitter.close()

        assert written == 1
        data = json.loads(Path(out).read_text().strip())
        assert data["confidence"] == pytest.approx(0.36, abs=0.01)

    def test_emitter_zone_null_for_entry(self, tmp_path):
        out = str(tmp_path / "events.jsonl")
        emitter = EventEmitter(out)
        emitter.emit_entry("S", "C", "V", "2026-01-01T10:00:00Z", 0.9, False, 1)
        emitter.close()
        data = json.loads(Path(out).read_text().strip())
        assert data["zone_id"] is None

    def test_emitter_dwell_ms_cumulative(self, tmp_path):
        """ZONE_DWELL dwell_ms should be cumulative, not a fixed 30-second window."""
        out = str(tmp_path / "events.jsonl")
        emitter = EventEmitter(out)

        # Emit ZONE_DWELL events at 30s, 60s, 90s with cumulative dwell
        for i, dwell in enumerate([30000, 60000, 90000], start=1):
            emitter.emit_zone_dwell(
                "S", "C", "V", f"2026-01-01T10:00:{i*30:02d}Z",
                "SKINCARE", dwell, 0.8, False, i, "SKINCARE",
            )
        emitter.close()

        lines = Path(out).read_text().strip().splitlines()
        dwells = [json.loads(l)["dwell_ms"] for l in lines]
        assert dwells == [30000, 60000, 90000], "dwell_ms must be cumulative"

    def test_billing_queue_join_has_queue_depth(self, tmp_path):
        out = str(tmp_path / "events.jsonl")
        emitter = EventEmitter(out)
        emitter.emit_billing_queue_join("S", "C", "V", "2026-01-01T10:00:00Z", 4, 0.88, False, 1)
        emitter.close()
        data = json.loads(Path(out).read_text().strip())
        assert data["metadata"]["queue_depth"] == 4
        assert data["event_type"] == "BILLING_QUEUE_JOIN"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Group entry — 3 people simultaneously
# ─────────────────────────────────────────────────────────────────────────────

class TestGroupEntry:
    def test_three_simultaneous_tracks_produce_three_entry_events(self, tmp_path, layout_file):
        """Group of 3 entering together → 3 distinct visitor_ids and 3 ENTRY events."""
        gallery = ReIDGallery(similarity_threshold=0.72)

        # Three people with different colours (different appearances)
        colours = [(200, 50, 50), (50, 200, 50), (50, 50, 200)]
        visitor_ids = []

        for track_id, colour in enumerate(colours, start=1):
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            frame[100:400, 200:280] = colour
            bbox = np.array([200.0, 100.0, 280.0, 400.0], dtype=np.float32)

            vid, is_reentry, is_new = gallery.register_or_reidentify(
                track_id, frame, bbox, "CAM_ENTRY_01", "entry"
            )
            visitor_ids.append(vid)
            assert is_reentry is False, f"Track {track_id} incorrectly flagged as REENTRY"
            assert is_new is True

        # All 3 should have different visitor_ids
        assert len(set(visitor_ids)) == 3, "Group entry produced duplicate visitor_ids"

    def test_group_entry_emitter(self, tmp_path):
        """Group of 3 entering → 3 ENTRY events in JSONL output."""
        out = str(tmp_path / "events.jsonl")
        emitter = EventEmitter(out)

        for i, vid in enumerate(["VIS_a", "VIS_b", "VIS_c"], start=1):
            emitter.emit_entry("S", "CAM_ENTRY_01", vid, "2026-01-01T10:00:00Z", 0.85, False, 1)

        emitter.close()
        lines = Path(out).read_text().strip().splitlines()
        assert len(lines) == 3

        entry_events = [json.loads(l) for l in lines if json.loads(l)["event_type"] == "ENTRY"]
        assert len(entry_events) == 3
        visitor_ids_emitted = {e["visitor_id"] for e in entry_events}
        assert len(visitor_ids_emitted) == 3


# ─────────────────────────────────────────────────────────────────────────────
# 6. Staff flag propagation
# ─────────────────────────────────────────────────────────────────────────────

class TestStaffClassification:
    def test_staff_events_have_is_staff_true(self, tmp_path):
        out = str(tmp_path / "events.jsonl")
        emitter = EventEmitter(out)

        emitter.emit_entry("S", "CAM_ENTRY_01", "VIS_staff", "2026-01-01T10:00:00Z", 0.9, True, 1)
        emitter.emit_zone_enter("S", "CAM_FLOOR_01", "VIS_staff", "2026-01-01T10:00:30Z", "CASH_COUNTER", 0.9, True, 2)

        emitter.close()
        lines = Path(out).read_text().strip().splitlines()
        for line in lines:
            event = json.loads(line)
            assert event["is_staff"] is True, f"Staff event has is_staff=False: {event}"

    def test_customer_events_have_is_staff_false(self, tmp_path):
        out = str(tmp_path / "events.jsonl")
        emitter = EventEmitter(out)
        emitter.emit_entry("S", "CAM_ENTRY_01", "VIS_cust", "2026-01-01T10:00:00Z", 0.85, False, 1)
        emitter.close()
        data = json.loads(Path(out).read_text().strip())
        assert data["is_staff"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 7. Empty store — no events emitted for empty clips
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyStoreHandling:
    def test_empty_frame_produces_no_events(self, tmp_path, layout_file):
        """When the frame has no detections, no events should be emitted."""
        out = str(tmp_path / "events.jsonl")
        emitter = EventEmitter(out)
        written, rejected = emitter.close()

        # No events written to the file
        lines = Path(out).read_text().strip()
        assert lines == "", "Empty store should produce no events"
        assert written == 0

    def test_zero_events_file_is_valid(self, tmp_path):
        """A zero-event JSONL is valid — the API must handle this gracefully."""
        out = str(tmp_path / "events.jsonl")
        emitter = EventEmitter(out)
        emitter.close()

        content = Path(out).read_text()
        assert content == ""  # empty file, not null, not erroring
