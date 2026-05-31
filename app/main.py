"""FastAPI application — RetailPulse Store Intelligence API + Web Dashboard."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import close_db, get_db, init_db
from .middleware import StructuredLoggingMiddleware
from .models import HealthResponse, StoreHealthInfo
from .routers import events as events_router
from .routers import metrics as metrics_router

_START_TIME = time.monotonic()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("api")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("RetailPulse API started")
    yield
    close_db()
    logger.info("RetailPulse API stopped")


app = FastAPI(
    title="RetailPulse Store Intelligence API",
    description="Real-time retail store analytics from CCTV event streams.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# Middleware
app.add_middleware(StructuredLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(events_router.router)
app.include_router(metrics_router.router)

# Static files (web dashboard)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Global exception handlers ─────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", "unknown")
    logger.error(
        "unhandled_exception trace_id=%s error_type=%s error=%s",
        trace_id, type(exc).__name__, exc,
    )
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "trace_id": trace_id},
    )


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def dashboard():
    """Serve the live analytics dashboard."""
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"message": "Dashboard not found. API is running at /api/docs"})


# ── Health endpoint ────────────────────────────────────────────────────────────

@app.post("/admin/reset", tags=["system"])
async def reset_db():
    """Wipe all events and visitor sessions. Used when reloading real data."""
    from .db import get_db
    conn = get_db()
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM visitor_sessions")
    conn.execute("DELETE FROM daily_snapshots")
    conn.commit()
    logger.info("DB reset — all events and sessions cleared")
    return {"reset": True}


@app.post("/admin/reload-pos", tags=["system"])
async def reload_pos():
    """Reload POS transactions from CSV into the DB (call after updating the CSV)."""
    from .db import POS_CSV, get_db
    import csv
    conn = get_db()
    conn.execute("DELETE FROM pos_transactions")
    rows_loaded = 0
    try:
        with open(POS_CSV) as f:
            for row in csv.DictReader(f):
                conn.execute(
                    "INSERT OR IGNORE INTO pos_transactions (transaction_id, store_id, timestamp, basket_value_inr) VALUES (?,?,?,?)",
                    (row["transaction_id"], row["store_id"], row["timestamp"], float(row.get("basket_value_inr", 0))),
                )
                rows_loaded += 1
        conn.commit()
    except Exception as exc:
        return {"error": str(exc), "loaded": 0}
    # Re-mark conversions for any billing sessions ingested before this POS load
    from .ingestion import recorrelate_conversions
    reconverted = recorrelate_conversions(conn)
    logger.info("POS reloaded: %d rows, %d sessions reconverted", rows_loaded, reconverted)
    return {"loaded": rows_loaded, "source": POS_CSV, "reconverted": reconverted}


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health(request: Request) -> HealthResponse:
    """Service health — last event per store, STALE_FEED detection (>10 min)."""
    from datetime import datetime, timezone
    trace_id = getattr(request.state, "trace_id", "unknown")

    try:
        conn     = get_db()
        db_status = "connected"

        rows = conn.execute(
            "SELECT store_id, MAX(timestamp) AS last_ts FROM events GROUP BY store_id"
        ).fetchall()

        now_dt = datetime.now(timezone.utc)
        stores_info: dict[str, StoreHealthInfo] = {}

        for row in rows:
            last_ts = row["last_ts"]
            feed_status = "NO_DATA"
            if last_ts:
                try:
                    last_dt = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                    age_s = (now_dt - last_dt).total_seconds()
                    feed_status = "STALE_FEED" if age_s > 600 else "OK"
                except ValueError:
                    feed_status = "OK"
            stores_info[row["store_id"]] = StoreHealthInfo(
                last_event_at=last_ts, feed_status=feed_status
            )

        if not stores_info:
            stores_info["_"] = StoreHealthInfo(last_event_at=None, feed_status="NO_DATA")

    except Exception as exc:
        logger.error("Health DB error trace_id=%s: %s", trace_id, exc)
        return JSONResponse(  # type: ignore[return-value]
            status_code=503,
            content={
                "status": "degraded",
                "db": "disconnected",
                "stores": {},
                "uptime_seconds": round(time.monotonic() - _START_TIME, 1),
            },
        )

    return HealthResponse(
        status="healthy",
        db=db_status,
        stores=stores_info,
        uptime_seconds=round(time.monotonic() - _START_TIME, 1),
    )
