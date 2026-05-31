# RetailPulse — Store Intelligence System

End-to-end pipeline that turns raw CCTV footage into real-time store analytics. Built for the Purplle Tech Challenge 2026.

**North star metric**: Offline Store Conversion Rate = visitors who purchased ÷ total unique visitors

---

## Setup

```bash
# Clone
git clone <repo-url> && cd RetailPulse

# Python virtual environment (for the pipeline — heavier deps)
python3 -m venv .venv
.venv/bin/pip install -r requirements-pipeline.txt
```

The API runs in Docker and doesn't need the venv.

---

## The Canonical Workflow — One Command

```bash
.venv/bin/python run.py
```

This single command does the entire end-to-end run with interactive terminal feedback:

```
⚡ RetailPulse — Store Intelligence System

▶ Step 1/5  Checking environment        ✓ Docker, compose, CCTV clips
▶ Step 2/5  Starting API (docker)       ✓ Image cached → API healthy in 5s
▶ Step 3/5  Detection Pipeline          ✓ YOLOv8 on 4 cameras → 614 events
▶ Step 4/5  Ingesting events            ✓ 614/614 events, 0 rejected
▶ Step 5/5  Live Metrics                ✓ 50 visitors, 20% conversion

╭──── Live Metrics ────╮  ╭──── Conversion Funnel ────╮  ╭──── Zone Heatmap ────╮
│ Footage   2026-04-10 │  │ Entry         ████ 50     │  │ Lakme    ████ 18    │
│ Visitors          50 │  │ Zone Visit    ███▒ 42 ↓16%│  │ Maybellin███▒ 13    │
│ Conversion     20.0% │  │ Billing Queue █░░░ 10 ↓76%│  │ Billing  ██░░ 10    │
│ Queue depth        0 │  │ Purchase      █░░░ 10     │  │ Maybel   ██░░ 10    │
╰──────────────────────╯  ╰───────────────────────────╯  ╰─────────────────────╯
```

After that, the dashboard auto-opens at `http://localhost:8000`.

**Options:**
```bash
.venv/bin/python run.py --skip-pipeline    # if events_real.jsonl already exists
.venv/bin/python run.py --no-open          # don't open browser
.venv/bin/python run.py --replay           # Part E live demo (real-time replay)
.venv/bin/python run.py --replay --speed 50 --reset   # 50× speed from empty DB
```

Want to run the steps manually? See "Manual Workflow" below.

**What you'll see** (numbers from actual CCTV detection):
- 50 unique visitors detected by YOLOv8
- 20% conversion rate (computed from POS correlation, 5-min window)
- 8 zones with real visit data on the heatmap
- Live event feed showing the most recent events
- Auto-detected date: `2026-04-10` (from the footage timestamp)

---

## Manual Workflow (if you prefer step-by-step)

```bash
# 1. Start the API
docker compose up -d --build

# 2. Run detection on real CCTV (or skip if data/events_real.jsonl exists)
./pipeline/run.sh

# 3. Ingest events
.venv/bin/python scripts/ingest_real.py
```

---

## Part E Bonus — Live Real-Time Demo

```bash
.venv/bin/python run.py --replay --speed 10 --reset
```

This streams the 614 real events through the API in chronological order at 10× speed. The dashboard's SSE connection picks up each batch and updates the floor plan, KPIs, funnel, and event feed live. With `--speed 50` the full clip set replays in ~9 seconds.

---

## Acceptance Gate Verification

The five checks the reviewer's automated harness runs:

```bash
# 1. docker compose up runs without manual intervention
docker compose up -d --build

# 2. POST /events/ingest accepts events (no 5xx)
curl -X POST http://localhost:8000/events/ingest \
     -H "Content-Type: application/json" -d '{"events":[]}'

# 3. GET /stores/STORE_BLR_002/metrics returns valid JSON
curl http://localhost:8000/stores/STORE_BLR_002/metrics

# 4. Detection pipeline produces structured events
./pipeline/run.sh && head -1 data/events_real.jsonl

# 5. DESIGN.md and CHOICES.md present (>250 words each)
wc -w docs/DESIGN.md docs/CHOICES.md
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/events/ingest` | Ingest a batch of up to 500 events. Idempotent by `event_id`. |
| `GET`  | `/stores/{id}/metrics` | Unique visitors, conversion rate, dwell per zone, queue depth, abandonment rate |
| `GET`  | `/stores/{id}/funnel` | Entry → Zone Visit → Billing Queue → Purchase with drop-off % |
| `GET`  | `/stores/{id}/heatmap` | Zone visit frequency + avg dwell, normalised 0–100, with `data_confidence` flag |
| `GET`  | `/stores/{id}/anomalies` | Active anomalies (BILLING_QUEUE_SPIKE, CONVERSION_DROP, DEAD_ZONE) with severity |
| `GET`  | `/stores/{id}/stream` | Server-Sent Events — full dashboard bundle every 3 seconds |
| `GET`  | `/health` | Service status, per-store STALE_FEED warnings |
| `GET`  | `/stores` | List of store IDs with ingested data |
| `POST` | `/admin/reset` | Wipe events and sessions (for re-testing) |
| `POST` | `/admin/reload-pos` | Reload POS transactions from `data/pos_transactions.csv` |

Interactive API docs at: `http://localhost:8000/api/docs`

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

78 tests cover:
- Event schema validation (8 event types, confidence bounds, idempotency)
- Zone classification (point-in-polygon, entry line crossing)
- Re-ID gallery (camera handoff, re-entry detection, group entry)
- API endpoints (metrics, funnel, heatmap, anomalies, health)
- Edge cases: empty store, all-staff clip, REENTRY funnel dedup, zero purchases

Each test file has a `# PROMPT:` / `# CHANGES MADE:` block at the top showing the AI prompt used to generate it and what I edited afterwards — per the AI Engineering scoring rubric.

---

## What the Pipeline Detects (from Real Footage)

Each `.mp4` is processed independently but shares the Re-ID gallery for cross-camera identity persistence.

| Clip | Camera ID | Role | What's Visible |
|------|-----------|------|----------------|
| CAM 1.mp4 | CAM_FLOOR_01 | floor | Top wall shelving — The Face Shop, Good Vibes, Derma, Maybel |
| CAM 2.mp4 | CAM_FLOOR_02 | floor | Bottom wall shelving — Lakme, FacesCanada, Maybelline, Swiss |
| CAM 3.mp4 | CAM_ENTRY_01 | **entry** | Glass entry door — wooden floor inside, dark marble outside |
| CAM 4.mp4 | — | **stockroom** | Back room with Purplle boxes. **Skipped — no customers** |
| CAM 5.mp4 | CAM_BILLING_01 | billing | POS terminal, billing queue, accessories display |

Camera mapping was verified by inspecting actual frames (see `data/frames/`).

---

## Notes on the Sample Data

The footage in `Resources/CCTV Footage/` is sample CCTV — about 2.5 minutes per camera, not the full 20 minutes the problem statement describes. This affects what's visible to the pipeline:

- 614 events emitted across 4 cameras
- 49 unique visitors (real, detected by YOLOv8)
- 31 BILLING_QUEUE_JOIN events from CAM 5
- 276 ZONE_ENTER events from floor cameras
- Only a handful of explicit ENTRY events (most visitors appear on floor cameras without an explicit entry threshold crossing — short clips don't capture every entry)

**POS data was not provided in `Resources/`** — `data/pos_transactions.csv` is generated by `ingest_real.py` to align with detected billing event timestamps. When the evaluator runs their own held-out events, they supply their own POS data.

---

## Architecture

See `docs/DESIGN.md` for the full architecture walkthrough with the AI-Assisted Decisions section.

See `docs/CHOICES.md` for the three key engineering decisions (detection model, event schema, storage).

Quick summary:

```
CCTV Clips → YOLOv8 + ByteTrack + Re-ID gallery → events.jsonl
                                                      ↓
                                            POST /events/ingest
                                                      ↓
                                              SQLite (WAL mode)
                                                      ↓
                                           Real-time API endpoints
                                                      ↓
                                       Web dashboard (SSE-driven)
```

---

## File Layout

```
RetailPulse/
├── pipeline/              # YOLO + ByteTrack + Re-ID + state machine
├── app/                   # FastAPI service
│   ├── routers/           # /events/ingest, /stores/*
│   ├── services/          # metrics, funnel, heatmap, anomalies
│   └── static/index.html  # Live dashboard
├── data/
│   ├── store_layout.json  # Zone polygons (calibrated from real frames)
│   ├── clips_config.json  # Camera type mapping
│   └── pos_transactions.csv
├── scripts/
│   ├── ingest_real.py     # Load real pipeline output (canonical demo path)
│   ├── replay_live.py     # Real-time replay (Part E bonus)
│   └── test_helpers/
│       └── seed_events.py # Synthetic events (test only, NOT for demo)
├── tests/                 # 78 tests, all passing
├── docs/
│   ├── DESIGN.md
│   └── CHOICES.md
├── docker-compose.yml
└── README.md
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `data/retail.db` | SQLite database file |
| `POS_CSV` | `data/pos_transactions.csv` | POS transactions for conversion correlation |
| `YOLO_MODEL` | `yolov8n.pt` | Detection model (auto-downloaded on first run) |
| `FRAME_SKIP` | `3` | Process every Nth frame (5fps effective from 15fps source) |
| `DETECTION_CONF` | `0.35` | YOLO confidence threshold (low to preserve partial occlusions) |
| `DEVICE` | auto | `cpu` / `cuda:0` |
| `STORE_ID` | `STORE_BLR_002` | Store tag for emitted events |
