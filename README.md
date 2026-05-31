# RetailPulse — Store Intelligence System

End-to-end retail analytics pipeline. Starts from raw CCTV footage and produces a live store intelligence API with a real-time web dashboard.

**North star metric**: Offline Store Conversion Rate = visitors who purchased ÷ total unique visitors

---

## Quick Start — 3 Commands

```bash
# 1. Start the API
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# 2. Load data (generates events, aligns POS transactions, ingests everything)
.venv/bin/python scripts/seed_events.py --api http://localhost:8000

# 3. Open the dashboard
xdg-open http://localhost:8000
```

The dashboard shows live metrics as soon as events are ingested. No manual date configuration needed — it auto-detects the most recent date with data.

---

## Full Setup (Docker — Acceptance Gate)

```bash
# Clone and enter
git clone <repo-url> && cd store-intelligence

# Start everything
docker compose up --build

# Load data
python scripts/seed_events.py --api http://localhost:8000

# Verify
curl http://localhost:8000/health
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

---

## Running the Detection Pipeline on Real CCTV Clips

Install pipeline dependencies (separate from API — heavier, needs GPU optional):

```bash
pip install -r requirements-pipeline.txt
```

Run on all 4 customer cameras (CAM 4 is a stockroom and is automatically skipped):

```bash
./pipeline/run.sh
```

This processes approximately 10 minutes of footage across 4 cameras and writes `data/events_real.jsonl`.

What happens during processing:
- **CAM 3** (entry/exit glass door): detects entry/exit crossings, re-entries
- **CAM 1** (brand shelf wall): The Face Shop, Good Vibes, Derma zone visits
- **CAM 2** (colour cosmetics): Lakme, FacesCanada, Maybelline zone visits
- **CAM 5** (billing counter): billing queue joins, abandonments, queue depth
- **CAM 4** skipped — it's the stockroom, no customer-facing area

Ingest the real events:

```bash
python3 - << 'EOF'
import json, urllib.request
events = [json.loads(l) for l in open('data/events_real.jsonl')]
for i in range(0, len(events), 200):
    batch = events[i:i+200]
    req = urllib.request.Request(
        'http://localhost:8000/events/ingest',
        json.dumps({'events': batch}).encode(),
        {'Content-Type': 'application/json'}
    )
    print(json.loads(urllib.request.urlopen(req).read()))
EOF
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/events/ingest` | Batch ingest up to 500 events. Idempotent by `event_id`. |
| `GET`  | `/stores/{id}/metrics` | Unique visitors, conversion rate, avg dwell per zone, queue depth, abandonment rate |
| `GET`  | `/stores/{id}/funnel` | Entry → Zone Visit → Billing Queue → Purchase with drop-off % |
| `GET`  | `/stores/{id}/heatmap` | Zone visit frequency + avg dwell, normalised 0–100 |
| `GET`  | `/stores/{id}/anomalies` | Active anomalies: BILLING_QUEUE_SPIKE, CONVERSION_DROP, DEAD_ZONE |
| `GET`  | `/stores/{id}/stream` | Server-Sent Events — live dashboard bundle every 3s |
| `GET`  | `/health` | Service status, last event per store, STALE_FEED warnings |
| `GET`  | `/stores` | List store IDs with ingested data |

**Interactive API docs**: `http://localhost:8000/api/docs`

Verify all endpoints:
```bash
STORE=STORE_BLR_002
curl http://localhost:8000/health
curl "http://localhost:8000/stores/$STORE/metrics"
curl "http://localhost:8000/stores/$STORE/funnel"
curl "http://localhost:8000/stores/$STORE/heatmap"
curl "http://localhost:8000/stores/$STORE/anomalies"
curl "http://localhost:8000/events/stores/$STORE/recent?limit=10"
```

---

## Running Tests

```bash
# All 78 tests
.venv/bin/python -m pytest tests/ -v

# With coverage
.venv/bin/python -m pytest tests/ --cov=app --cov=pipeline --cov-report=term-missing

# Individual suites
.venv/bin/python -m pytest tests/test_pipeline.py     # 33 pipeline tests
.venv/bin/python -m pytest tests/test_metrics.py      # 24 API tests
.venv/bin/python -m pytest tests/test_anomalies.py    # 11 anomaly tests
.venv/bin/python -m pytest tests/test_api_assertions.py  # 10 spec assertions
```

---

## System Architecture

```
CCTV Clips (1080p, 4 cameras)
        │
        ▼
┌───────────────────────────┐
│    Detection Pipeline     │
│  YOLOv8n + ByteTrack      │
│  Re-ID gallery (HSV emb)  │
│  Zone classification      │
│  Staff detection (HSV)    │
└──────────┬────────────────┘
           │ events_real.jsonl
           ▼
┌───────────────────────────┐
│  POST /events/ingest      │
│  Pydantic validation      │
│  INSERT OR IGNORE (dedup) │
│  Session state upserts    │
│  POS correlation          │
└──────────┬────────────────┘
           ▼
┌───────────────────────────┐
│  SQLite (WAL mode)        │
│  events, visitor_sessions │
│  pos_transactions         │
└──────────┬────────────────┘
           ▼
┌───────────────────────────┐
│  Analytics API (FastAPI)  │
│  /metrics /funnel         │
│  /heatmap /anomalies      │
│  /stream (SSE)            │
└──────────┬────────────────┘
           ▼
┌───────────────────────────┐
│  Web Dashboard            │
│  SVG floor plan heatmap   │
│  Live funnel + anomalies  │
│  Real-time event feed     │
└───────────────────────────┘
```

See `docs/DESIGN.md` for detailed architecture and AI-Assisted Decisions.
See `docs/CHOICES.md` for engineering decision rationale.

---

## Key Design Decisions

**Detection model**: YOLOv8n at confidence threshold 0.35. Low threshold preserves low-confidence detections (partial occlusion in billing area) — the `confidence` field on each event carries the uncertainty forward rather than silently dropping detections.

**Re-ID**: Gallery-based appearance matching using combined HSV histograms (torso + upper body). Handles three scenarios: brief occlusion (ByteTrack), camera handoff (entry → floor → billing), and genuine re-entry (5-minute gallery TTL).

**Session model**: One session per visitor per day. REENTRY events extend the session, not create new ones — this is what makes the funnel correct (a visitor who exits and re-enters still counts as 1 in the funnel, not 2).

**Storage**: SQLite with WAL mode. The access pattern is single-writer + multiple concurrent readers, which is exactly what WAL is optimised for. PostgreSQL would be correct at 40 stores live; SQLite is correct for this scope.

**Date handling**: All services use `effective_date()` — returns the most recent date with actual event data, not today's date. This means the dashboard works regardless of when footage was recorded.

---

## Repository Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py          # YOLO + ByteTrack + full state machine
│   ├── tracker.py         # Re-ID gallery and visitor_id persistence
│   ├── zones.py           # Zone polygon classification (shapely)
│   ├── staff.py           # Staff detection (HSV colour + trajectory)
│   ├── emit.py            # Event schema validation + JSONL writer
│   ├── config.py          # All tunable parameters
│   ├── run.py             # CLI entrypoint
│   ├── run.sh             # One-command pipeline runner
│   └── bytetrack.yaml     # ByteTrack tracker configuration
├── app/
│   ├── main.py            # FastAPI app + health endpoint
│   ├── models.py          # Pydantic event schema (single source of truth)
│   ├── db.py              # SQLite setup, schema, POS loading
│   ├── ingestion.py       # Ingest + dedup + session state
│   ├── middleware.py      # trace_id injection + structured logging
│   ├── routers/
│   │   ├── events.py      # POST /events/ingest
│   │   └── metrics.py     # Analytics endpoints + SSE stream
│   ├── services/
│   │   ├── metrics.py     # Real-time metrics computation
│   │   ├── funnel.py      # Session-based funnel
│   │   ├── heatmap.py     # Zone heatmap normalisation
│   │   ├── anomalies.py   # Three anomaly detectors
│   │   └── utils.py       # Shared utilities (effective_date)
│   └── static/
│       └── index.html     # Premium web dashboard (SSE-driven)
├── tests/
│   ├── test_pipeline.py       # 33 pipeline tests (schema, zones, Re-ID)
│   ├── test_metrics.py        # 24 API tests (metrics, funnel, heatmap)
│   ├── test_anomalies.py      # 11 anomaly detection tests
│   ├── test_api_assertions.py # 10 spec assertions
│   └── conftest.py            # Shared fixtures
├── data/
│   ├── store_layout.json  # Zone polygons (calibrated from real footage)
│   ├── clips_config.json  # Camera type mapping for each clip
│   └── pos_transactions.csv
├── scripts/
│   └── seed_events.py     # Generate + ingest demo data instantly
├── docs/
│   ├── DESIGN.md          # Architecture + AI-Assisted Decisions
│   └── CHOICES.md         # Three engineering decisions with full reasoning
├── DESIGN.md              # (copy — also at root for visibility)
├── CHOICES.md             # (copy — also at root for visibility)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt           # API dependencies
├── requirements-pipeline.txt  # Pipeline dependencies (heavier)
└── README.md
```

---

## Environment Variables

**API** (docker-compose.yml or shell):

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `data/retail.db` | SQLite database path |
| `POS_CSV` | `data/pos_transactions.csv` | POS transactions file |

**Pipeline** (shell):

| Variable | Default | Description |
|----------|---------|-------------|
| `YOLO_MODEL` | `yolov8n.pt` | Detection model (swap to `yolov9c.pt` for better accuracy) |
| `FRAME_SKIP` | `3` | Process every Nth frame (3 = 5fps from 15fps source) |
| `DETECTION_CONF` | `0.35` | Detection confidence threshold |
| `DEVICE` | auto | `cpu` or `cuda:0` |
| `STORE_ID` | `STORE_BLR_002` | Store ID tagged on events |

---

## Camera Mapping

Verified by frame-by-frame inspection of actual footage:

| File | Camera ID | Type | What it shows |
|------|-----------|------|---------------|
| CAM 1.mp4 | CAM_FLOOR_01 | floor | Brand shelf wall (The Face Shop, Good Vibes, Derma, Maybel) |
| CAM 2.mp4 | CAM_FLOOR_02 | floor | Colour cosmetics (Lakme, FacesCanada, Maybelline, Swiss Beauty) |
| CAM 3.mp4 | CAM_ENTRY_01 | entry | Glass entry/exit door — wood floor inside, dark marble outside |
| CAM 4.mp4 | CAM_STOCKROOM_01 | stockroom | Back room with Purplle boxes — **skipped, no customers** |
| CAM 5.mp4 | CAM_BILLING_01 | billing | POS terminal, billing queue, accessories display |
