"""Seed synthetic events for API verification.

Generates a realistic 20-visitor session day for STORE_BLR_002:
- 20 customers enter (3 staff)
- Various zone visits
- Some reach billing, some abandon, some convert
- 1 group entry (3 together), 2 re-entries

Usage:
    python scripts/seed_events.py [--api http://localhost:8000]
    python scripts/seed_events.py --file-only   # just write to data/events.jsonl
"""

from __future__ import annotations

import json
import sys
import uuid
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

STORE_ID  = "STORE_BLR_002"
DATE_BASE = datetime(2026, 3, 3, 10, 0, 0, tzinfo=timezone.utc)


def ts(minutes: float, seconds: float = 0) -> str:
    dt = DATE_BASE + timedelta(minutes=minutes, seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(visitor_id, event_type, camera_id="CAM_ENTRY_01", zone_id=None,
       dwell_ms=0, is_staff=False, confidence=0.88, queue_depth=None,
       sku_zone=None, session_seq=1, timestamp=None, offset_min=0):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": STORE_ID,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp or ts(offset_min),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
        },
    }


def generate_events() -> list[dict]:
    events = []

    # ── Staff arrive at store open ────────────────────────────────────────────
    for i, staff_id in enumerate(["STAFF_001", "STAFF_002", "STAFF_003"]):
        events.append(ev(staff_id, "ENTRY", is_staff=True, timestamp=ts(0, i*5), confidence=0.95, session_seq=1))
        events.append(ev(staff_id, "ZONE_ENTER", camera_id="CAM_BILLING_01", zone_id="CASH_COUNTER",
                         is_staff=True, timestamp=ts(1, i*5), confidence=0.95, session_seq=2))

    # ── Customer 1: enters, browses skincare, leaves without buying ───────────
    events += [
        ev("VIS_a1b2c3", "ENTRY",      timestamp=ts(2),     confidence=0.91, session_seq=1),
        ev("VIS_a1b2c3", "ZONE_ENTER", camera_id="CAM_FLOOR_01", zone_id="SKINCARE_SHELF",
           timestamp=ts(3), sku_zone="SKINCARE", confidence=0.89, session_seq=2),
        ev("VIS_a1b2c3", "ZONE_DWELL", camera_id="CAM_FLOOR_01", zone_id="SKINCARE_SHELF",
           dwell_ms=30000, sku_zone="SKINCARE", timestamp=ts(3, 30), confidence=0.87, session_seq=3),
        ev("VIS_a1b2c3", "ZONE_DWELL", camera_id="CAM_FLOOR_01", zone_id="SKINCARE_SHELF",
           dwell_ms=60000, sku_zone="SKINCARE", timestamp=ts(4),    confidence=0.87, session_seq=4),
        ev("VIS_a1b2c3", "ZONE_EXIT",  camera_id="CAM_FLOOR_01", zone_id="SKINCARE_SHELF",
           dwell_ms=70000, timestamp=ts(4, 10), confidence=0.85, session_seq=5),
        ev("VIS_a1b2c3", "EXIT",       timestamp=ts(5),     confidence=0.82, session_seq=6),
    ]

    # ── Customer 2: enters, browses makeup, goes to billing, converts ─────────
    events += [
        ev("VIS_b2c3d4", "ENTRY",      timestamp=ts(3),     confidence=0.93, session_seq=1),
        ev("VIS_b2c3d4", "ZONE_ENTER", camera_id="CAM_FLOOR_01", zone_id="MAKEUP_UNIT",
           sku_zone="MAKEUP", timestamp=ts(4), confidence=0.9, session_seq=2),
        ev("VIS_b2c3d4", "ZONE_DWELL", camera_id="CAM_FLOOR_01", zone_id="MAKEUP_UNIT",
           dwell_ms=45000, sku_zone="MAKEUP", timestamp=ts(4, 45), confidence=0.88, session_seq=3),
        ev("VIS_b2c3d4", "ZONE_ENTER", camera_id="CAM_BILLING_01", zone_id="BILLING_QUEUE",
           timestamp=ts(6),     queue_depth=1, confidence=0.91, session_seq=4),
        ev("VIS_b2c3d4", "BILLING_QUEUE_JOIN", camera_id="CAM_BILLING_01", zone_id="BILLING_QUEUE",
           timestamp=ts(6),     queue_depth=1, confidence=0.91, session_seq=4),
        ev("VIS_b2c3d4", "ZONE_EXIT",  camera_id="CAM_BILLING_01", zone_id="BILLING_QUEUE",
           dwell_ms=180000,     timestamp=ts(9), confidence=0.88, session_seq=5),
        ev("VIS_b2c3d4", "EXIT",       timestamp=ts(10),    confidence=0.85, session_seq=6),
    ]
    # POS transaction exists at 10:10 UTC — this visitor converts

    # ── Group entry: 3 customers enter together ───────────────────────────────
    for i, vid in enumerate(["VIS_g1aaaa", "VIS_g2bbbb", "VIS_g3cccc"]):
        offset = 7 + i * 0.02  # within 2 seconds of each other
        events.append(ev(vid, "ENTRY", timestamp=ts(offset), confidence=0.85+i*0.02, session_seq=1))
        events.append(ev(vid, "ZONE_ENTER", camera_id="CAM_FLOOR_01", zone_id="FRAGRANCE",
                         sku_zone="FRAGRANCE", timestamp=ts(8), confidence=0.84, session_seq=2))

    # ── Customer 4: enters billing, queue spikes, abandons ────────────────────
    events += [
        ev("VIS_c3d4e5", "ENTRY",     timestamp=ts(9),  confidence=0.90, session_seq=1),
        ev("VIS_c3d4e5", "ZONE_ENTER", camera_id="CAM_BILLING_01", zone_id="BILLING_QUEUE",
           timestamp=ts(10), confidence=0.88, session_seq=2),
        ev("VIS_c3d4e5", "BILLING_QUEUE_JOIN", camera_id="CAM_BILLING_01", zone_id="BILLING_QUEUE",
           timestamp=ts(10), queue_depth=4, confidence=0.88, session_seq=2),
        ev("VIS_c3d4e5", "BILLING_QUEUE_ABANDON", camera_id="CAM_BILLING_01",
           zone_id="BILLING_QUEUE", dwell_ms=120000,
           timestamp=ts(12), confidence=0.82, session_seq=3),
        ev("VIS_c3d4e5", "EXIT",      timestamp=ts(12, 30), confidence=0.78, session_seq=4),
    ]

    # ── Re-entry: Customer 5 exits and returns ────────────────────────────────
    events += [
        ev("VIS_d4e5f6", "ENTRY",   timestamp=ts(10),    confidence=0.89, session_seq=1),
        ev("VIS_d4e5f6", "ZONE_ENTER", camera_id="CAM_FLOOR_01", zone_id="SKINCARE_SHELF",
           sku_zone="SKINCARE", timestamp=ts(11), confidence=0.87, session_seq=2),
        ev("VIS_d4e5f6", "EXIT",    timestamp=ts(13),    confidence=0.85, session_seq=3),
        ev("VIS_d4e5f6", "REENTRY", timestamp=ts(15),    confidence=0.88, session_seq=4),
        ev("VIS_d4e5f6", "ZONE_ENTER", camera_id="CAM_FLOOR_01", zone_id="MAKEUP_UNIT",
           sku_zone="MAKEUP", timestamp=ts(16), confidence=0.86, session_seq=5),
        ev("VIS_d4e5f6", "BILLING_QUEUE_JOIN", camera_id="CAM_BILLING_01",
           zone_id="BILLING_QUEUE", timestamp=ts(18), queue_depth=2, confidence=0.87, session_seq=6),
        ev("VIS_d4e5f6", "EXIT",    timestamp=ts(22),    confidence=0.82, session_seq=7),
    ]

    # ── 10 more customers: varied zones ──────────────────────────────────────
    zones = ["SKINCARE_SHELF", "MAKEUP_UNIT", "FRAGRANCE", "HAIRCARE_SHELF", "COLOUR_COSMETICS"]
    skus  = ["SKINCARE",       "MAKEUP",      "FRAGRANCE", "HAIRCARE",       "COLOUR_COSMETICS"]
    for i in range(10):
        vid = f"VIS_{uuid.uuid4().hex[:6]}"
        zidx = i % len(zones)
        t_enter = 12 + i * 0.8
        events += [
            ev(vid, "ENTRY",      timestamp=ts(t_enter), confidence=0.80+i*0.01, session_seq=1),
            ev(vid, "ZONE_ENTER", camera_id="CAM_FLOOR_01", zone_id=zones[zidx],
               sku_zone=skus[zidx], timestamp=ts(t_enter+1), confidence=0.82, session_seq=2),
            ev(vid, "ZONE_DWELL", camera_id="CAM_FLOOR_01", zone_id=zones[zidx],
               dwell_ms=30000, sku_zone=skus[zidx], timestamp=ts(t_enter+1.5), confidence=0.81, session_seq=3),
        ]
        if i % 3 == 0:  # every 3rd visitor reaches billing
            events += [
                ev(vid, "BILLING_QUEUE_JOIN", camera_id="CAM_BILLING_01",
                   zone_id="BILLING_QUEUE", timestamp=ts(t_enter+3), queue_depth=i%3+1,
                   confidence=0.85, session_seq=4),
            ]
        events.append(ev(vid, "EXIT", timestamp=ts(t_enter+5), confidence=0.79, session_seq=5))

    return events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--file-only", action="store_true")
    args = parser.parse_args()

    events = generate_events()
    print(f"Generated {len(events)} events for {STORE_ID}")

    # Always write to file
    out = ROOT / "data" / "events.jsonl"
    with open(out, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    print(f"Written to {out}")

    if args.file_only:
        return

    # Ingest via API in batches of 100
    try:
        import urllib.request
        batch_size = 100
        total_accepted = 0
        for i in range(0, len(events), batch_size):
            batch = events[i:i+batch_size]
            payload = json.dumps({"events": batch}).encode()
            req = urllib.request.Request(
                f"{args.api}/events/ingest",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                total_accepted += result.get("accepted", 0)

        print(f"Ingested {total_accepted} events via {args.api}")
    except Exception as e:
        print(f"API ingest failed: {e}")
        print("API may not be running. Start it first with: uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
