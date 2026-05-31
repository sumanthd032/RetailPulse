# RetailPulse — Store Intelligence System

End-to-end pipeline that turns raw CCTV footage into real-time store analytics. Built for the Purplle Tech Challenge 2026.

**North star metric**: Offline Store Conversion Rate = visitors who purchased ÷ total unique visitors

---

## The Canonical Workflow — One Command

Everything runs in Docker. The only prerequisite is **Docker** (with Compose v2 — `docker compose`, ships with Docker Desktop). No Python, no virtualenv, no `pip install` on the host.

```bash
git clone https://github.com/sumanthd032/RetailPulse
cd RetailPulse

docker compose up --build
```

This single command — identical on macOS, Linux, and Windows — comes up in about a minute:

1. **`api`** builds the FastAPI service and comes up healthy at `http://localhost:8000`.
2. **`ingest`** (a one-shot container) waits for the API, then ingests the detection events that ship with the repo (`data/events_real.jsonl`), generates aligned POS data, and prints the live metrics. It exits `0`; the API keeps serving the dashboard.

```
retailpulse-api     | INFO  Uvicorn running on http://0.0.0.0:8000
retailpulse-ingest  | POS reloaded: 6 transactions
retailpulse-ingest  | INGESTED: 398 real events   REJECTED: 0   FOOTAGE DATE: 2026-04-10
retailpulse-ingest  |   Visitors: 51   Conversion: 21.6%   Queue depth: 0
retailpulse-ingest exited with code 0
```

Then open the dashboard at **`http://localhost:8000`**.

> The events are pre-computed from the real CCTV footage and committed to the repo, so the demo is fast and doesn't need PyTorch or a GPU. To regenerate them from the raw video yourself, see **Regenerating events** below.

To run detached and stop:

```bash
docker compose up -d --build    # start in the background
docker compose down             # stop everything
```

**What you'll see** (numbers from actual CCTV detection):
- 51 unique visitors detected by YOLOv8
- 21.6% conversion rate (computed from POS correlation, 5-min window)
- 8 zones with real visit data on the heatmap
- Live event feed showing the most recent events
- Auto-detected date: `2026-04-10` (from the footage timestamp)

---

## Regenerating Events from Raw CCTV (opt-in)

The detection events are committed so the demo runs fast, but the full YOLOv8 pipeline is here and reproducible. It's behind a Compose **profile** because it pulls a multi-GB PyTorch image and runs CPU inference for a few minutes — so it never slows down the default run.

```bash
# API must be up first (docker compose up -d), then:
docker compose --profile detect run --rm pipeline
```

This runs detection on all four cameras, overwrites `data/events_real.jsonl`, and re-ingests. The detection model (`yolov8n.pt`) is baked into the image, so it runs offline. On a GPU host, use the host runner below instead for CUDA acceleration.

---

## Optional: Host Runner (`run.py`)

`docker compose up` is the canonical path and needs nothing but Docker. If you'd rather run it on the host — for a nicer live terminal UI, GPU acceleration, or the Part E real-time replay — there's a Python runner. It needs a virtualenv with the pipeline deps:

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-pipeline.txt
python run.py
```

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-pipeline.txt
python run.py
```

> If PowerShell blocks the activation script, allow it for the current user once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

`run.py` orchestrates the same five steps with a rich progress UI and auto-opens the browser. Options:

```bash
python run.py --skip-pipeline    # if data/events_real.jsonl already exists
python run.py --no-open          # don't open browser
python run.py --replay           # Part E live demo (real-time replay)
python run.py --replay --speed 50 --reset   # 50× speed from empty DB
```

To run the detection pipeline on its own on the host: `./pipeline/run.sh` (macOS / Linux) or `.\pipeline\run.ps1` (Windows).

---

## Part E Bonus — Live Real-Time Demo

Using the host runner (see "Optional: Host Runner" above):

```bash
python run.py --replay --speed 10 --reset
```

This streams the real events through the API in chronological order at 10× speed. The dashboard's SSE connection picks up each batch and updates the floor plan, KPIs, funnel, and event feed live. With `--speed 50` the full clip set replays in ~9 seconds.

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

# 4. Detection pipeline produces structured events (events ship in the repo;
#    this regenerates them from the raw footage)
docker compose --profile detect run --rm pipeline && head -1 data/events_real.jsonl

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

Run on the host, in the same venv as the [host runner](#optional-host-runner-runpy):

```bash
pip install pytest          # not bundled in the runtime images
python -m pytest tests/ -v
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

- 398 events emitted across 4 cameras (the copy committed to the repo)
- 51 unique visitors (real, detected by YOLOv8)
- 21 BILLING_QUEUE_JOIN events from CAM 5
- 175 ZONE_ENTER events from floor cameras
- Only a handful of explicit ENTRY events (most visitors appear on floor cameras without an explicit entry threshold crossing — short clips don't capture every entry)

(Exact counts vary slightly if you regenerate on different hardware — CPU vs GPU detection differs. These are the numbers in the shipped `data/events_real.jsonl`.)

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
│   ├── run.sh             # Pipeline runner (macOS / Linux)
│   └── run.ps1            # Pipeline runner (Windows / PowerShell)
├── app/                   # FastAPI service
│   ├── routers/           # /events/ingest, /stores/*
│   ├── services/          # metrics, funnel, heatmap, anomalies
│   └── static/index.html  # Live dashboard
├── data/
│   ├── store_layout.json  # Zone polygons (calibrated from real frames)
│   ├── clips_config.json  # Camera type mapping
│   ├── events_real.jsonl  # Pre-computed detection output (shipped for fast demo)
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
├── Dockerfile             # API image (lightweight)
├── Dockerfile.pipeline    # Detection image (PyTorch + Ultralytics, opt-in)
├── docker-compose.yml     # api + ingest (default); pipeline behind --profile detect
├── run.py                 # Optional host runner (rich UI, Part E replay)
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
