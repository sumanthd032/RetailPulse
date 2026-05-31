"""Store analytics router — metrics, funnel, heatmap, anomalies, SSE stream."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..models import AnomalyResponse, FunnelResponse, HeatmapResponse, MetricsResponse
from ..services.anomalies import compute_anomalies
from ..services.funnel import compute_funnel
from ..services.heatmap import compute_heatmap
from ..services.metrics import compute_metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stores", tags=["analytics"])


# ── GET /stores ───────────────────────────────────────────────────────────────

@router.get("")
def list_stores():
    """List all store IDs that have ingested events."""
    from ..db import get_db
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT store_id FROM events ORDER BY store_id"
    ).fetchall()
    stores = [r["store_id"] for r in rows]
    if not stores:
        stores = ["STORE_BLR_002"]
    return {"stores": stores}


# ── GET /stores/{id}/metrics ──────────────────────────────────────────────────

@router.get("/{store_id}/metrics", response_model=MetricsResponse)
def store_metrics(store_id: str, date: Optional[str] = None) -> MetricsResponse:
    """Real-time store metrics — never cached, always computed fresh.

    Returns 0.0 for all numeric fields if no events have been ingested.
    Excludes is_staff=true events from all counts.
    """
    return compute_metrics(store_id, date)


# ── GET /stores/{id}/funnel ───────────────────────────────────────────────────

@router.get("/{store_id}/funnel", response_model=FunnelResponse)
def store_funnel(store_id: str, date: Optional[str] = None) -> FunnelResponse:
    """Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.

    Session is the unit of analysis — REENTRY events do not create new sessions
    and do not inflate the entry count.
    """
    return compute_funnel(store_id, date)


# ── GET /stores/{id}/heatmap ──────────────────────────────────────────────────

@router.get("/{store_id}/heatmap", response_model=HeatmapResponse)
def store_heatmap(store_id: str, date: Optional[str] = None) -> HeatmapResponse:
    """Zone visit frequency and avg dwell, normalised 0–100 per dimension.

    data_confidence is False when fewer than 20 sessions exist — the heatmap
    built on sparse data is statistically noisy and should be shown with a caveat.
    """
    return compute_heatmap(store_id, date)


# ── GET /stores/{id}/anomalies ────────────────────────────────────────────────

@router.get("/{store_id}/anomalies", response_model=AnomalyResponse)
def store_anomalies(store_id: str) -> AnomalyResponse:
    """Active operational anomalies: BILLING_QUEUE_SPIKE, CONVERSION_DROP, DEAD_ZONE.

    Each anomaly includes severity (INFO/WARN/CRITICAL) and a specific
    suggested_action string. data_confidence is False when historical baseline
    is insufficient (< 2 days of history).
    """
    return compute_anomalies(store_id)


# ── GET /stores/{id}/stream — Server-Sent Events ──────────────────────────────

@router.get("/{store_id}/stream")
async def store_stream(store_id: str, request: Request):
    """Server-Sent Events stream — pushes combined dashboard bundle every 3s.

    The bundle includes metrics, funnel, heatmap, anomalies, and recent events.
    Frontend connects once and receives live updates without polling.
    """
    async def generate():
        loop = asyncio.get_event_loop()
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected for %s", store_id)
                break
            try:
                bundle = await loop.run_in_executor(
                    None, lambda: _build_stream_bundle(store_id)
                )
                yield f"data: {json.dumps(bundle)}\n\n"
            except Exception as exc:
                logger.error("SSE bundle error for %s: %s", store_id, exc)
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _build_stream_bundle(store_id: str) -> dict:
    """Compute the full dashboard data bundle for SSE push."""
    from ..db import get_db
    from ..services.utils import effective_date
    conn = get_db()

    # Use the most recent date with actual data — not today's date.
    # This ensures the dashboard always shows real numbers regardless of
    # when the events were ingested.
    date = effective_date(store_id, conn)

    metrics   = compute_metrics(store_id, date)
    funnel    = compute_funnel(store_id, date)
    heatmap   = compute_heatmap(store_id, date)
    anomalies = compute_anomalies(store_id)

    rows = conn.execute(
        """
        SELECT event_id, visitor_id, event_type, timestamp,
               zone_id, confidence, is_staff
        FROM events
        WHERE store_id = ?
        ORDER BY ingested_at DESC, timestamp DESC
        LIMIT 25
        """,
        (store_id,),
    ).fetchall()
    recent = [dict(r) for r in rows]

    has_data = metrics.unique_visitors > 0 or len(recent) > 0

    return {
        "store_id":      store_id,
        "effective_date": date,
        "has_data":      has_data,
        "timestamp":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metrics":       metrics.model_dump(),
        "funnel":        funnel.model_dump(),
        "heatmap":       heatmap.model_dump(),
        "anomalies":     anomalies.model_dump(),
        "recent_events": recent,
    }
