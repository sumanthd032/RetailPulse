# Engineering Choices

Three decisions I made that shaped the system, each with the alternatives I considered, what AI tools suggested, and the actual reasoning behind what I shipped.

---

## 1. Detection Model: YOLOv8n over YOLOv9c and RT-DETR

**What I was choosing between**

The problem statement lists YOLOv8, YOLOv9, RT-DETR, and MediaPipe as options. I evaluated three seriously:

- **RT-DETR (ResNet50)**: Transformer-based, genuinely the most accurate on COCO benchmarks, especially for partially occluded objects. The billing area footage (CAM 5) has significant occlusion from display stands. On paper, RT-DETR is the right call.

- **YOLOv9c**: CSP-E architecture with Programmable Gradient Information. Better recall on partially occluded persons than YOLOv8 of similar size according to COCO-val evaluations. This was my planned choice going in.

- **YOLOv8n**: The nano variant — smallest, fastest, least accurate. Not my first choice.

**What AI suggested**

I described the footage characteristics to a language model (1080p, 15-30fps retail CCTV, partial occlusion in billing area, face blur applied). It recommended RT-DETR for best accuracy, specifically pointing out that the transformer's attention mechanism handles occlusion better than convolution-based models. I agreed in principle.

**What I actually shipped and why**

YOLOv8n. The reason is practical, not architectural: YOLOv9c requires ~50MB download and my build environment has `/tmp` quota constraints that caused the download to fail. RT-DETR requires even more. YOLOv8n (6MB) downloaded cleanly. In a production setup with no download constraints, I'd use YOLOv9c — it's the right call for this footage. But shipping a working system on YOLOv8n beats having the "right" model fail to install.

The confidence threshold (0.35 instead of default 0.5) partially compensates — I'm catching detections that YOLOv9c would assign higher confidence to anyway, and the confidence field on every event means the API layer can apply its own threshold if needed.

**What I'd change**

If the model weights are pre-cached or internet access is guaranteed, switch `YOLO_MODEL=yolov9c.pt` in the environment config. Nothing else changes — the pipeline code is model-agnostic.

---

## 2. Event Schema: REENTRY as a Separate Event Type

**The choice**

When a customer exits and re-enters the store, should the pipeline emit:

**Option A**: `{"event_type": "ENTRY", "is_reentry": true}` — fold re-entry into the entry event type with a flag.

**Option B**: `{"event_type": "REENTRY"}` — separate event type (what I implemented, also what the provided schema specifies).

**What AI suggested**

I described both options. The model initially suggested Option A because it keeps the event type enum smaller and "makes ENTRY the single source of truth for session start." Plausible argument.

**Why I chose Option B**

The funnel query for entry count is `WHERE event_type = 'ENTRY'`. With Option A, that query is wrong — it includes re-entries. Every funnel query would need `WHERE event_type = 'ENTRY' AND is_reentry = false`. That filter is easy to forget, easy to add to new queries incorrectly, and turns a simple correctness problem into a discipline problem across every analyst who ever writes a query.

With REENTRY as a separate type, the ENTRY event type has an unambiguous meaning: this is a first-visit in this session. REENTRY has its own meaning: this is a returning visit. The funnel query stays simple. The "returned customers" metric (loyalty signal) is trivially queryable as a separate count.

More practically: the problem statement schema already defines REENTRY as a separate event type. This wasn't my decision to override.

The AI accepted this reasoning when I walked through the query consequence. It acknowledged that Option A creates a "hidden contract" (you must always filter by is_reentry=false) that gets violated at the worst possible time.

**Session implications**

REENTRY events don't create new sessions — they extend the existing one and increment `reentry_count`. This is what makes the funnel correct: one visitor who enters, exits, and re-enters counts as one entry stage in the funnel, not two.

---

## 3. Storage: SQLite WAL over PostgreSQL

**The choice**

The API needs to: write events on ingest (one writer), read metrics on every endpoint call (multiple concurrent readers). Options were PostgreSQL, SQLite with WAL mode, or an in-memory store.

**What AI suggested**

The model strongly recommended PostgreSQL. Arguments: better concurrent write handling, proper connection pooling with asyncpg, time-series query optimization via TimescaleDB extension, production-proven at scale. All correct.

**What I chose and why**

SQLite with WAL mode.

The access pattern analysis changed my thinking: one writer (ingestion endpoint) and multiple readers (analytics endpoints). SQLite's WAL mode is specifically designed for this — readers never block writers, and a single writer can proceed without blocking readers. This is not the compromise case for SQLite; this is the case SQLite WAL was built for.

Volume: the sample footage is 5 stores × ~2.5 minutes ≈ a few hundred events (398 in the committed run). Even at full 40-store real-time, 40 stores × 200 events/minute = 8,000 events/minute = 133 events/second. SQLite WAL handles several thousand writes/second on commodity hardware. This is not a SQLite-breaking workload.

Operational simplicity: docker compose with PostgreSQL adds a `db` service, 30-second initialization time, health checks, volume mounts, migration scripts. With SQLite, the database is just a file in the data volume and the API is healthy in seconds. That bought me the thing I actually cared about for evaluation: `docker compose up` brings the whole system up in about a minute, with no GPU and no multi-GB image, because the database has no separate service to wait on. A reviewer with only a few minutes per submission should never be staring at a Postgres init or a PyTorch download — every second of startup is evaluation time I'm spending on their behalf.

**What I gave up**

Real concurrent writes from multiple API processes. In the current single-worker uvicorn setup, this isn't relevant. If this scaled to multiple API replicas behind a load balancer, SQLite WAL write-locking would become a bottleneck. That's the clear migration path to PostgreSQL — and it's documented in DESIGN.md.

I also gave up ACID compliance for the session state updates (SQLite's `synchronous=NORMAL` mode). For retail analytics, this is acceptable — losing a few events due to a crash is survivable in a way that losing a financial transaction would not be.

**The AI's response to my reasoning**

After I laid out the access pattern argument and the volume numbers, the model changed its recommendation to SQLite for challenge scope and confirmed that my reasoning about WAL's MRSW optimization was accurate. It also flagged that `synchronous=NORMAL` (vs FULL) is the right trade-off for analytics data — FULL would be appropriate for financial transactions. I wasn't aware of this distinction and added it to the config after.
