"""Event ingestion router — POST /events/ingest."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from ..ingestion import ingest_events
from ..models import IngestRequest, IngestResponse, StoreEvent, IngestError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


@router.post("/ingest", response_model=IngestResponse)
def ingest(request: Request, body: IngestRequest) -> IngestResponse:
    """Ingest a batch of store events (max 500).

    Idempotent by event_id — safe to call twice with the same payload.
    Returns partial success (HTTP 200) even if some events in the batch are malformed.
    Malformed events are reported in the errors list, valid ones are ingested.
    """
    trace_id = getattr(request.state, "trace_id", "unknown")

    valid_events: list[StoreEvent] = []
    pre_errors: list[IngestError] = []

    for raw in body.events:
        # Per-event Pydantic validation — isolated so one bad event doesn't abort the batch
        try:
            ev = StoreEvent.model_validate(raw) if isinstance(raw, dict) else StoreEvent.model_validate(raw.model_dump())
            valid_events.append(ev)
        except (ValidationError, Exception) as exc:
            event_id = raw.get("event_id", "unknown") if isinstance(raw, dict) else getattr(raw, "event_id", "unknown")
            msg = str(exc.errors()[0]["msg"]) if isinstance(exc, ValidationError) else str(exc)
            pre_errors.append(IngestError(event_id=str(event_id), reason=msg))

    result = ingest_events(valid_events)
    result.rejected += len(pre_errors)
    result.errors   += pre_errors

    logger.info(
        "ingest trace_id=%s accepted=%d rejected=%d",
        trace_id, result.accepted, result.rejected,
    )
    return result


@router.get("/stores/{store_id}/recent", tags=["events"])
def recent_events(store_id: str, limit: int = 25):
    """Return the N most recently ingested events for a store."""
    from ..db import get_db
    conn = get_db()
    limit = min(max(1, limit), 100)
    rows = conn.execute(
        """
        SELECT event_id, visitor_id, event_type, timestamp,
               zone_id, confidence, is_staff, queue_depth
        FROM events
        WHERE store_id = ?
        ORDER BY ingested_at DESC, timestamp DESC
        LIMIT ?
        """,
        (store_id, limit),
    ).fetchall()
    return {"events": [dict(r) for r in rows]}
