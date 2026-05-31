"""Event ingestion — validate, deduplicate, update session state, POS correlate.

The contract:
  - Idempotent by event_id: ingesting the same event twice → same DB state
  - Partial success: one bad event in a batch of 500 doesn't reject the batch
  - Session updates happen only for *newly* inserted events (rowcount > 0)
  - POS correlation runs immediately on BILLING_QUEUE_JOIN insertion
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .db import get_db
from .models import EventType, IngestError, IngestResponse, StoreEvent

logger = logging.getLogger(__name__)


def _session_id(store_id: str, visitor_id: str, date: str) -> str:
    return f"{store_id}|{visitor_id}|{date}"


def _check_pos_correlation(
    conn: sqlite3.Connection,
    store_id: str,
    billing_ts: str,
    window_s: int = 300,
) -> bool:
    """Return True if any POS transaction exists within window_s of billing_ts."""
    row = conn.execute(
        """
        SELECT 1 FROM pos_transactions
        WHERE store_id = ?
          AND timestamp >= ?
          AND timestamp <= datetime(?, '+' || CAST(? AS TEXT) || ' seconds')
        LIMIT 1
        """,
        (store_id, billing_ts, billing_ts, window_s),
    ).fetchone()
    return row is not None


def _upsert_session(conn: sqlite3.Connection, event: StoreEvent) -> None:
    """Incrementally update visitor_sessions for a single newly-inserted event.

    All updates are idempotent:
    - Boolean flags use SET x = 1 (setting twice is fine)
    - last_event_at uses MAX(existing, new) to never go backwards
    - reentry_count increments per unique REENTRY event_id (dedup is upstream)
    """
    date = event.timestamp[:10]
    sid = _session_id(event.store_id, event.visitor_id, date)
    ts = event.timestamp
    staff = int(event.is_staff)

    # Ensure session row exists for all event types
    conn.execute(
        """
        INSERT OR IGNORE INTO visitor_sessions
          (id, store_id, visitor_id, date, is_staff, first_entry_at, last_event_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (sid, event.store_id, event.visitor_id, date, staff, ts, ts),
    )

    et = event.event_type

    if et == EventType.REENTRY:
        conn.execute(
            """
            UPDATE visitor_sessions SET
              reentry_count = reentry_count + 1,
              last_event_at = MAX(last_event_at, ?)
            WHERE id = ?
            """,
            (ts, sid),
        )

    elif et in (EventType.ZONE_ENTER, EventType.ZONE_EXIT, EventType.ZONE_DWELL):
        conn.execute(
            """
            UPDATE visitor_sessions SET
              reached_zone = 1,
              last_event_at = MAX(last_event_at, ?)
            WHERE id = ?
            """,
            (ts, sid),
        )

    elif et == EventType.BILLING_QUEUE_JOIN:
        conn.execute(
            """
            UPDATE visitor_sessions SET
              reached_zone    = 1,
              reached_billing = 1,
              last_event_at   = MAX(last_event_at, ?)
            WHERE id = ?
            """,
            (ts, sid),
        )
        # Immediate POS correlation
        if _check_pos_correlation(conn, event.store_id, ts):
            conn.execute(
                "UPDATE visitor_sessions SET converted = 1 WHERE id = ?",
                (sid,),
            )

    elif et == EventType.BILLING_QUEUE_ABANDON:
        conn.execute(
            """
            UPDATE visitor_sessions SET
              abandoned_billing = 1,
              last_event_at     = MAX(last_event_at, ?)
            WHERE id = ?
            """,
            (ts, sid),
        )

    else:  # ENTRY, EXIT
        conn.execute(
            "UPDATE visitor_sessions SET last_event_at = MAX(last_event_at, ?) WHERE id = ?",
            (ts, sid),
        )


def ingest_events(events: list[StoreEvent]) -> IngestResponse:
    """Ingest a validated batch of events.

    Steps:
    1. INSERT OR IGNORE each event (dedup by primary key event_id)
    2. If rowcount > 0 (truly new): run session state update
    3. Commit once at the end — all-or-nothing per batch for consistency
    """
    conn = get_db()
    ingested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    accepted = 0
    rejected = 0
    errors: list[IngestError] = []

    for event in events:
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO events
                  (event_id, store_id, camera_id, visitor_id, event_type,
                   timestamp, zone_id, dwell_ms, is_staff, confidence,
                   queue_depth, sku_zone, session_seq, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.store_id,
                    event.camera_id,
                    event.visitor_id,
                    event.event_type,
                    event.timestamp,
                    event.zone_id,
                    event.dwell_ms,
                    int(event.is_staff),
                    event.confidence,
                    event.metadata.queue_depth,
                    event.metadata.sku_zone,
                    event.metadata.session_seq,
                    ingested_at,
                ),
            )
            accepted += 1
            if cursor.rowcount > 0:
                _upsert_session(conn, event)

        except Exception as exc:
            logger.error("Insert failed for event %s: %s", event.event_id, exc)
            rejected += 1
            errors.append(IngestError(event_id=event.event_id, reason=str(exc)))

    conn.commit()

    logger.info(
        "ingest complete accepted=%d rejected=%d total=%d",
        accepted, rejected, accepted + rejected,
    )
    return IngestResponse(accepted=accepted, rejected=rejected, errors=errors)
