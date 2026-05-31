"""Real-time replay of pipeline events into the API.

Reads data/events_real.jsonl (or any events file) and POSTs events to the API
in chronological order, simulating the pipeline streaming events live. The
dashboard sees events arrive in real time and updates accordingly — proving
the system is genuinely connected, not batch-processed.

This is what powers the Part E bonus "live dashboard" demo.

Usage:
    .venv/bin/python scripts/replay_live.py --speed 10
    .venv/bin/python scripts/replay_live.py --file data/events_real.jsonl --speed 5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _post_batch(api: str, events: list[dict]) -> tuple[int, int]:
    if not events:
        return 0, 0
    req = urllib.request.Request(
        f"{api}/events/ingest",
        data=json.dumps({"events": events}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        result = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return result.get("accepted", 0), result.get("rejected", 0)
    except Exception as exc:
        print(f"  ERROR posting batch: {exc}", file=sys.stderr)
        return 0, 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file",  default=str(ROOT / "data" / "events_real.jsonl"),
                   help="JSONL events file to replay")
    p.add_argument("--api",   default="http://localhost:8000",
                   help="API base URL")
    p.add_argument("--speed", type=float, default=10.0,
                   help="Playback speed multiplier (10 = 10x faster than real time)")
    p.add_argument("--reset", action="store_true",
                   help="Reset DB before replay (fresh state)")
    args = p.parse_args()

    events_path = Path(args.file)
    if not events_path.exists():
        print(f"ERROR: {events_path} not found", file=sys.stderr)
        print("Run the pipeline first: ./pipeline/run.sh")
        return 1

    # Verify API
    try:
        urllib.request.urlopen(f"{args.api}/health", timeout=3).read()
    except Exception as exc:
        print(f"ERROR: API not reachable at {args.api}: {exc}", file=sys.stderr)
        return 1

    # Load events sorted by timestamp
    events = sorted(
        [json.loads(l) for l in events_path.open() if l.strip()],
        key=lambda e: e["timestamp"],
    )
    if not events:
        print("ERROR: no events in file")
        return 1

    print(f"Loaded {len(events)} events from {events_path.name}")
    print(f"Speed:  {args.speed}× real time")
    print(f"API:    {args.api}")

    if args.reset:
        req = urllib.request.Request(f"{args.api}/admin/reset", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
        print("DB reset — replay will start from empty state")

    first_ts = _parse_iso(events[0]["timestamp"])
    last_ts  = _parse_iso(events[-1]["timestamp"])
    span_s   = (last_ts - first_ts).total_seconds()
    replay_s = span_s / args.speed

    print(f"Time range: {first_ts.strftime('%H:%M:%S')} → {last_ts.strftime('%H:%M:%S')}")
    print(f"Real span:  {span_s:.0f}s  →  replay:  {replay_s:.0f}s")
    print(f"Watch the dashboard at {args.api} as events arrive live")
    print("─" * 60)

    # Group events into 1-second buckets (relative to first_ts), POST each bucket
    buckets: dict[int, list[dict]] = {}
    for e in events:
        offset_s = int((_parse_iso(e["timestamp"]) - first_ts).total_seconds())
        buckets.setdefault(offset_s, []).append(e)

    wall_start = time.monotonic()
    total_accepted = 0
    total_rejected = 0
    last_print_bucket = -1

    for offset_s in sorted(buckets):
        # Wait until this offset is due (scaled by speed)
        due_at = wall_start + offset_s / args.speed
        sleep_s = due_at - time.monotonic()
        if sleep_s > 0:
            time.sleep(sleep_s)

        batch = buckets[offset_s]
        acc, rej = _post_batch(args.api, batch)
        total_accepted += acc
        total_rejected += rej

        # Progress print every 10 simulated seconds
        if offset_s // 10 != last_print_bucket // 10:
            last_print_bucket = offset_s
            real_clock = (first_ts + timedelta(seconds=offset_s)).strftime("%H:%M:%S")
            print(f"  [+{offset_s:3d}s sim · {real_clock}]  posted {len(batch):3d} events  "
                  f"total: {total_accepted} accepted, {total_rejected} rejected")

    print("─" * 60)
    print(f"Replay complete")
    print(f"  Accepted: {total_accepted}")
    print(f"  Rejected: {total_rejected}")
    print(f"  Replay duration: {time.monotonic() - wall_start:.1f}s real time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
