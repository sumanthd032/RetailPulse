# RetailPulse — System Design

## What this system does

Apex Retail's offline stores generate zero behavioral data compared to their online channel. This system closes that gap. Starting from raw CCTV footage, it produces the same analytics a web team would consider basic: who entered, what they looked at, how long they spent there, whether they bought, and what's anomalous right now.

The north star is one number: **offline store conversion rate**. Every design decision I made was evaluated against whether it makes that number more accurate or more actionable.

## Architecture

```
CCTV Clips (1080p, 25-30fps)
       │
       ▼
┌─────────────────────────────────────┐
│         Detection Pipeline          │
│  YOLOv8n → ByteTrack → Re-ID       │
│  Zone classification (shapely)      │
│  Staff detection (HSV + trajectory) │
│  Entry threshold crossing           │
└──────────────┬──────────────────────┘
               │  events.jsonl (JSONL, 614 events from 4 cameras)
               ▼
┌─────────────────────────────────────┐
│       POST /events/ingest           │
│  Pydantic validation per event      │
│  INSERT OR IGNORE (event_id PK)     │
│  Session state upserts              │
│  POS correlation (5-min window)     │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│         SQLite (WAL mode)           │
│  events, visitor_sessions,          │
│  pos_transactions, daily_snapshots  │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│         FastAPI endpoints           │
│  /metrics  /funnel  /heatmap        │
│  /anomalies  /health  /stream (SSE) │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│      Web Dashboard (SSE-driven)     │
│  Floor plan heatmap, funnel,        │
│  live anomaly panel, event feed     │
└─────────────────────────────────────┘
```

## Detection Pipeline

The pipeline processes each clip independently but shares a Re-ID gallery across cameras for cross-camera identity persistence.

**Person detection**: YOLOv8n at confidence threshold 0.35. The threshold is intentionally lower than the default 0.5 — the billing area has significant partial occlusion from display stands, and I'd rather emit a low-confidence event than silently miss a person. The confidence field on every event carries this uncertainty forward rather than making an irreversible filter decision at the pipeline layer.

**Tracking**: ByteTrack with `high_thresh=0.6, low_thresh=0.1, track_buffer=45 frames`. The two-stage matching is the key: high-confidence detections anchor the matching, then low-confidence detections (partially occluded people) get a second chance to associate with existing tracks. This was specifically chosen for the billing area footage (CAM 5) where customers overlap near the counter.

**Re-ID**: When ByteTrack loses a track (person exits frame or is fully occluded), the track ID resets on reappearance. My Re-ID gallery holds appearance embeddings (combined HSV histograms of torso + upper body) with a 5-minute TTL. When a new track appears at the entry camera, gallery lookup via cosine similarity determines if it's a re-entry or a new visitor. Same logic handles cross-camera handoffs (entry camera → floor camera → billing camera).

**Zone classification**: Shapely point-in-polygon on the bottom-centre of each bounding box (foot position, not centroid). Zone polygons were calibrated by frame inspection of each camera — the config stores fractional coordinates (0.0–1.0) so they work regardless of resolution changes.

**Staff detection**: Two signals combined. Primary: HSV colour histogram of the torso region matched against a reference uniform colour auto-calibrated from the first 30 frames where detections appear near the cash counter. Secondary: tracks spending >60% of time in staff-designated zones over 5+ minutes. The billing camera footage (CAM 5) yielded a calibrated staff colour of H=20.3, S=127.8, V=131.5 — a warm orange-brown consistent with the Purplle staff polo.

**Camera mapping** (critical — got this wrong initially, corrected from actual footage):
- CAM 1: Main brand shelf wall (The Face Shop, Good Vibes, Derma, Maybel)
- CAM 2: Colour cosmetics section (Lakme, FacesCanada, Maybelline, Swiss Beauty)
- CAM 3: Entry/exit glass door — the only entry camera
- CAM 4: Back room/stockroom — Purplle boxes, no customers, **excluded from pipeline**
- CAM 5: Billing counter with POS terminal

## API and Session State

The session is the unit of analysis, not the raw event. `visitor_sessions` is updated incrementally as events arrive:

- ENTRY creates a session row (INSERT OR IGNORE for idempotency)
- REENTRY increments `reentry_count` on the existing session — does not create a new row
- ZONE_ENTER sets `reached_zone = 1` (monotonic, idempotent)
- BILLING_QUEUE_JOIN sets `reached_billing = 1` and immediately checks POS correlation
- BILLING_QUEUE_ABANDON sets `abandoned_billing = 1`

This makes the funnel query a simple COUNT query on boolean flags rather than a per-visitor event reconstruction at query time. The funnel answer is always O(sessions) not O(events).

POS correlation: a visitor who had a BILLING_QUEUE_JOIN event, with a POS transaction in the same store within 5 minutes after their billing join timestamp, is marked `converted = 1`. No customer_id matching — it's purely time-window + store.

## Anomaly Detection

Three detectors run synchronously on each `/anomalies` request:

1. **BILLING_QUEUE_SPIKE**: Current queue depth (from most recent BILLING_QUEUE_JOIN) vs 7-day rolling average for the same hour and day-of-week. Severity: WARN at 1.5× baseline, CRITICAL at 2.5×. Bootstrap: if fewer than 2 days of history, uses absolute thresholds (WARN at depth≥5, CRITICAL at depth≥8) with `data_confidence=false`.

2. **CONVERSION_DROP**: Today's conversion rate vs 7-day average for the same weekday. Only fires after the store has been open 2+ hours with 5+ visitors (avoids false alarms at opening). CRITICAL at >40% drop, WARN at >20%.

3. **DEAD_ZONE**: Any zone with no ZONE_ENTER events in the past 30 minutes during store hours. Always INFO — could be product placement issue, not necessarily a camera problem.

## AI-Assisted Decisions

**1. Re-ID approach — LLM caught a critical oversight**

My initial plan was to use FaceNet or DeepFace for Re-ID — face-based identity matching is the obvious approach and produces better accuracy than body appearance alone. When I described this to a language model, it asked "the dataset description says full-face blur is applied to every frame — are you sure face-based Re-ID is viable?" I had missed this sentence in the problem statement. Went back, confirmed: "Full-face blur applied to every frame." Pivoted entirely to body appearance embedding (HSV histograms of torso region). The model was right to flag it and I accepted the correction immediately.

**2. Anomaly thresholds — I overrode the AI suggestion**

When I asked about setting queue depth thresholds for the BILLING_QUEUE_SPIKE anomaly, the model suggested `queue_depth > 10 = CRITICAL`. I pushed back: a small beauty store's billing area might physically hold at most 6–8 people. An absolute threshold of 10 would never fire in practice. I changed it to a relative threshold — current vs 7-day rolling average for the same hour — which catches a spike even if the absolute numbers are small. The model agreed this was more robust after I explained the store size constraint.

**3. CAM 4 classification — LLM helped, but I caught the error first**

I initially included CAM 4 in the pipeline with camera_type="floor". After running it and seeing the output (events from what appeared to be an empty room), I inspected the frames: Purplle branded cardboard boxes, a swivel chair, a water cooler. Clearly the stockroom. I asked the model to help me write the skip logic — it suggested checking camera_type in the config, which is what I implemented. The insight (it's a stockroom) was mine from frame inspection; the model helped with the clean implementation.

## What I'd Change for 40 Stores at Scale

1. **Storage**: SQLite WAL is excellent for this data volume (5 stores, 2.5-min clips). For 40 stores emitting events in real time, switch to PostgreSQL with TimescaleDB for time-series aggregations. The session state updates would need a proper connection pool (asyncpg).

2. **Re-ID**: The HSV histogram approach works in controlled retail lighting. In stores with more variable lighting or similar staff/customer clothing, switch to OSNet or a purpose-trained re-ID model. The gallery interface is designed to be swappable.

3. **Anomaly baselines**: The 7-day rolling average is computed at query time from raw events. At 40 stores × real-time, pre-compute hourly baselines in a background job and cache them. The current approach would be too slow.
