"""FastAPI application — RetailPulse Store Intelligence API.

Startup initialises SQLite. All endpoints are registered here.
Phase 1: health endpoint live. Phase 2: all remaining endpoints.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .db import close_db, get_db, init_db
from .middleware import StructuredLoggingMiddleware
from .models import HealthResponse, StoreHealthInfo

_START_TIME = time.monotonic()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("RetailPulse API started")
    yield
    close_db()
    logger.info("RetailPulse API stopped")


app = FastAPI(
    title="RetailPulse Store Intelligence API",
    description="Real-time store analytics from CCTV footage events.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(StructuredLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Global exception handlers ─────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", "unknown")
    logger.error("unhandled_exception trace_id=%s error=%s: %s", trace_id, type(exc).__name__, exc)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "trace_id": trace_id},
    )


# ── Health endpoint ────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health(request: Request) -> HealthResponse:
    """Service health — last event timestamp per store, STALE_FEED detection."""
    trace_id = getattr(request.state, "trace_id", "unknown")

    try:
        conn = get_db()
        db_status = "connected"

        # Last event per store
        rows = conn.execute(
            "SELECT store_id, MAX(timestamp) AS last_ts FROM events GROUP BY store_id"
        ).fetchall()

        import time as _time
        from datetime import datetime, timezone

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        stores_info: dict[str, StoreHealthInfo] = {}

        STALE_THRESHOLD_S = 600  # 10 minutes

        for row in rows:
            sid = row["store_id"]
            last_ts = row["last_ts"]
            feed_status = "NO_DATA"

            if last_ts:
                try:
                    last_dt = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                    now_dt = datetime.now(timezone.utc)
                    age_s = (now_dt - last_dt).total_seconds()
                    feed_status = "STALE_FEED" if age_s > STALE_THRESHOLD_S else "OK"
                except ValueError:
                    feed_status = "OK"

            stores_info[sid] = StoreHealthInfo(
                last_event_at=last_ts,
                feed_status=feed_status,
            )

        # If no events yet, show a placeholder
        if not stores_info:
            stores_info["_"] = StoreHealthInfo(last_event_at=None, feed_status="NO_DATA")

    except Exception as exc:
        logger.error("Health check DB error trace_id=%s: %s", trace_id, exc)
        return JSONResponse(  # type: ignore[return-value]
            status_code=503,
            content={
                "status": "degraded",
                "db": "disconnected",
                "stores": {},
                "uptime_seconds": round(time.monotonic() - _START_TIME, 1),
                "trace_id": trace_id,
            },
        )

    return HealthResponse(
        status="healthy",
        db=db_status,
        stores=stores_info,
        uptime_seconds=round(time.monotonic() - _START_TIME, 1),
    )


# ── Import and register Phase 2 routers (stubbed until Phase 2) ──────────────
# These are imported here so the app starts cleanly even if not yet implemented.
try:
    from .routers import events, metrics  # noqa: F401
    app.include_router(events.router)
    app.include_router(metrics.router)
    logger.info("Phase 2 routers registered")
except ImportError:
    logger.info("Phase 2 routers not yet available — health endpoint only")
