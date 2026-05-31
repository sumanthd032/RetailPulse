# RetailPulse — Store Intelligence System

End-to-end retail analytics: raw CCTV footage → live store intelligence API.

## Quick Start (5 commands)

```bash
# 1. Clone and enter
git clone <repo-url> && cd store-intelligence

# 2. Start the API
docker compose up --build -d

# 3. Install pipeline dependencies (CPU — GPU optional)
pip install -r requirements-pipeline.txt

# 4. Run the detection pipeline against the CCTV clips
./pipeline/run.sh

# 5. Ingest generated events into the API
curl -s -X POST http://localhost:8000/events/ingest \
     -H "Content-Type: application/json" \
     -d "{\"events\": $(python -c "
import json, sys
lines = open('data/events.jsonl').readlines()[:500]
print(json.dumps([json.loads(l) for l in lines]))
")}"
```

After step 4, `data/events.jsonl` contains all detected events. After step 5, the API serves live metrics.

## Verify

```bash
# Health check
curl http://localhost:8000/health

# Store metrics (after ingestion)
curl http://localhost:8000/stores/STORE_BLR_002/metrics

# Conversion funnel
curl http://localhost:8000/stores/STORE_BLR_002/funnel

# Zone heatmap
curl http://localhost:8000/stores/STORE_BLR_002/heatmap

# Active anomalies
curl http://localhost:8000/stores/STORE_BLR_002/anomalies
```

## Architecture

```
CCTV Clips
    ↓
pipeline/run.py          YOLOv9c detection + ByteTrack + Re-ID gallery
    ↓
data/events.jsonl        Structured events (ENTRY, EXIT, ZONE_ENTER, ...)
    ↓
POST /events/ingest      Validates, deduplicates, updates session state
    ↓
SQLite (WAL mode)        Events + visitor_sessions + POS transactions
    ↓
GET /stores/{id}/*       Real-time metrics, funnel, heatmap, anomalies
```

## Pipeline Details

The detection pipeline processes each clip independently:
- **Detection**: YOLOv9c at confidence 0.35 (low threshold to capture partial occlusion; confidence field preserved)
- **Tracking**: ByteTrack (high_thresh=0.6, low_thresh=0.1, buffer=45 frames)
- **Re-ID**: Appearance gallery with cosine similarity matching — handles re-entries and cross-camera dedup
- **Staff**: HSV colour histogram on torso region + trajectory pattern analysis
- **Events**: 8 event types emitted, all validated against Pydantic schema before write

## Running Tests

```bash
pip install pytest pytest-cov
pytest tests/ -v --cov=app --cov=pipeline --cov-report=term-missing
```

## Configuration

Key env vars for the API:
| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `data/retail.db` | SQLite database path |
| `POS_CSV` | `data/pos_transactions.csv` | POS transaction data |

Key env vars for the pipeline:
| Variable | Default | Description |
|----------|---------|-------------|
| `YOLO_MODEL` | `yolov9c.pt` | YOLO model (auto-downloaded) |
| `FRAME_SKIP` | `3` | Process every Nth frame |
| `DETECTION_CONF` | `0.35` | Detection confidence threshold |
| `DEVICE` | auto | `cpu` or `cuda:0` |
| `STORE_ID` | `STORE_BLR_002` | Store ID for events |

## File Layout

```
├── pipeline/           Detection pipeline (runs on host, needs GPU optional)
│   ├── detect.py       Main orchestrator: YOLO + ByteTrack + state machine
│   ├── tracker.py      Re-ID gallery and visitor_id persistence
│   ├── zones.py        Zone polygon classification
│   ├── staff.py        Staff vs customer classification
│   ├── emit.py         Event schema + JSONL writer
│   ├── config.py       All tunable parameters
│   └── run.sh          One-command pipeline runner
├── app/                FastAPI API (runs in Docker)
│   ├── main.py         App entrypoint + health endpoint
│   ├── models.py       Pydantic event schema (single source of truth)
│   └── db.py           SQLite schema + connection
├── data/
│   ├── store_layout.json   Zone polygon definitions per camera
│   ├── clips_config.json   Maps clip files to camera types
│   └── events.jsonl        Pipeline output (gitignored)
├── tests/              Unit + integration tests
├── docker-compose.yml
└── README.md
```
