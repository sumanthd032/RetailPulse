"""Shared test fixtures for RetailPulse pipeline and API tests."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Generator

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Synthetic frame fixtures ──────────────────────────────────────────────────

@pytest.fixture
def blank_frame() -> np.ndarray:
    """1080p blank frame for testing without a real video."""
    return np.zeros((1080, 1920, 3), dtype=np.uint8)


@pytest.fixture
def small_frame() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


@pytest.fixture
def person_frame(blank_frame) -> tuple[np.ndarray, np.ndarray]:
    """Frame with a synthetic person-coloured rectangle and its bbox."""
    frame = blank_frame.copy()
    # Simulate a person-shaped region with a distinctive colour
    frame[200:600, 800:900] = [0, 120, 200]  # BGR
    bbox = np.array([800, 200, 900, 600], dtype=np.float32)
    return frame, bbox


# ── Zone manager fixture ──────────────────────────────────────────────────────

@pytest.fixture
def layout_file(tmp_path) -> str:
    layout = {
        "store_id": "STORE_TEST",
        "cameras": {
            "CAM_ENTRY_01": {
                "type": "entry",
                "entry_line": {"x1_frac": 0.40, "y1_frac": 0.0, "x2_frac": 0.40, "y2_frac": 1.0},
                "inbound_side": "right",
                "zones": [
                    {
                        "zone_id": "ENTRY_AREA",
                        "sku_zone": None,
                        "is_staff_zone": False,
                        "polygon_frac": [[0.0, 0.0], [0.45, 0.0], [0.45, 1.0], [0.0, 1.0]],
                    }
                ],
            },
            "CAM_FLOOR_01": {
                "type": "floor",
                "entry_line": None,
                "zones": [
                    {
                        "zone_id": "SKINCARE_SHELF",
                        "sku_zone": "SKINCARE",
                        "is_staff_zone": False,
                        "polygon_frac": [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]],
                    },
                    {
                        "zone_id": "MAKEUP_UNIT",
                        "sku_zone": "MAKEUP",
                        "is_staff_zone": False,
                        "polygon_frac": [[0.5, 0.0], [1.0, 0.0], [1.0, 0.5], [0.5, 0.5]],
                    },
                ],
            },
            "CAM_BILLING_01": {
                "type": "billing",
                "entry_line": None,
                "zones": [
                    {
                        "zone_id": "BILLING_QUEUE",
                        "sku_zone": None,
                        "is_staff_zone": False,
                        "polygon_frac": [[0.0, 0.0], [0.7, 0.0], [0.7, 1.0], [0.0, 1.0]],
                    },
                    {
                        "zone_id": "CASH_COUNTER",
                        "sku_zone": None,
                        "is_staff_zone": True,
                        "polygon_frac": [[0.7, 0.0], [1.0, 0.0], [1.0, 1.0], [0.7, 1.0]],
                    },
                ],
            },
        },
    }
    path = tmp_path / "store_layout.json"
    path.write_text(json.dumps(layout))
    return str(path)


# ── Event JSONL fixture ───────────────────────────────────────────────────────

@pytest.fixture
def sample_events_jsonl(tmp_path) -> str:
    """Write a set of well-formed events to a temp JSONL file."""
    import uuid
    events = [
        {
            "event_id": str(uuid.uuid4()),
            "store_id": "STORE_TEST",
            "camera_id": "CAM_ENTRY_01",
            "visitor_id": "VIS_aaa001",
            "event_type": "ENTRY",
            "timestamp": "2026-03-03T10:05:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.88,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
        },
        {
            "event_id": str(uuid.uuid4()),
            "store_id": "STORE_TEST",
            "camera_id": "CAM_FLOOR_01",
            "visitor_id": "VIS_aaa001",
            "event_type": "ZONE_ENTER",
            "timestamp": "2026-03-03T10:05:30Z",
            "zone_id": "SKINCARE_SHELF",
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.82,
            "metadata": {"queue_depth": None, "sku_zone": "SKINCARE", "session_seq": 2},
        },
    ]
    path = tmp_path / "test_events.jsonl"
    with open(path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return str(path)


# ── In-memory SQLite fixture ──────────────────────────────────────────────────

@pytest.fixture
def test_db(tmp_path) -> Generator[str, None, None]:
    """Creates a fresh SQLite DB and patches app.db.DB_PATH."""
    import app.db as db_module

    db_path = str(tmp_path / "test.db")
    original = db_module.DB_PATH
    db_module.DB_PATH = db_path
    db_module._conn = None

    db_module.init_db()
    yield db_path

    db_module.close_db()
    db_module.DB_PATH = original
