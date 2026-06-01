"""Quick diagnostic for a pipeline events file: visitor counts, cross-camera
linking, and staff flags. Usage: python scripts/analyze_events.py <events.jsonl>"""
import json
import sys
from collections import Counter, defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "data/events_real.jsonl"
events = [json.loads(l) for l in open(path) if l.strip()]

types = Counter(e["event_type"] for e in events)
all_ids = set()
staff_ids = set()
per_cam_ids = defaultdict(set)
id_cameras = defaultdict(set)  # visitor_id -> set of cameras it appears on

for e in events:
    v = e.get("visitor_id")
    if not v:
        continue
    all_ids.add(v)
    cam = e.get("camera_id", "?")
    per_cam_ids[cam].add(v)
    id_cameras[v].add(cam)
    if e.get("is_staff"):
        staff_ids.add(v)

customer_ids = all_ids - staff_ids
multi_cam = {v: cams for v, cams in id_cameras.items() if len(cams) > 1}

print(f"file: {path}")
print(f"total events: {len(events)}")
print(f"event types: {dict(types)}")
print(f"distinct visitor_ids: {len(all_ids)}")
print(f"  flagged staff:    {len(staff_ids)}")
print(f"  customers:        {len(customer_ids)}")
print(f"per-camera distinct ids: {{ {', '.join(f'{c}: {len(s)}' for c, s in sorted(per_cam_ids.items()))} }}")
print(f"sum of per-camera ids: {sum(len(s) for s in per_cam_ids.values())}  (== distinct means NO cross-camera linking)")
print(f"ids appearing on >1 camera (real handoffs): {len(multi_cam)}")
for v, cams in list(multi_cam.items())[:15]:
    print(f"    {v}: {sorted(cams)}")
