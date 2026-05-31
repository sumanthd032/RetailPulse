"""Real-time store metrics — computed fresh from SQLite on every request."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..db import get_db
from ..models import MetricsResponse, ZoneDwellMetric
from .utils import effective_date

logger = logging.getLogger(__name__)


def compute_metrics(store_id: str, date: Optional[str] = None) -> MetricsResponse:
    """Compute real-time store metrics. Never returns nulls — always returns 0.0."""
    conn = get_db()
    date = effective_date(store_id, conn, date)

    # ── Unique customer visitors ──────────────────────────────────────────────
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM visitor_sessions
        WHERE store_id = ? AND date = ? AND is_staff = 0
        """,
        (store_id, date),
    ).fetchone()
    unique_visitors: int = (row["cnt"] or 0) if row else 0

    # ── Conversion rate ───────────────────────────────────────────────────────
    conv = conn.execute(
        """
        SELECT
          COUNT(*)       AS total,
          SUM(converted) AS converted_count
        FROM visitor_sessions
        WHERE store_id = ? AND date = ? AND is_staff = 0
        """,
        (store_id, date),
    ).fetchone()

    if conv and conv["total"] and conv["total"] > 0:
        conversion_rate = float(conv["converted_count"] or 0) / conv["total"]
    else:
        conversion_rate = 0.0

    # ── Avg dwell per zone (cumulative: max dwell_ms per visitor per zone) ────
    zone_rows = conn.execute(
        """
        SELECT
          zone_id,
          COUNT(DISTINCT visitor_id) AS visit_count,
          AVG(max_dwell)             AS avg_dwell_ms
        FROM (
          SELECT zone_id, visitor_id, MAX(dwell_ms) AS max_dwell
          FROM events
          WHERE store_id = ?
            AND event_type = 'ZONE_DWELL'
            AND is_staff   = 0
            AND date(timestamp) = ?
            AND zone_id IS NOT NULL
          GROUP BY zone_id, visitor_id
        )
        GROUP BY zone_id
        ORDER BY avg_dwell_ms DESC
        """,
        (store_id, date),
    ).fetchall()

    dwell_metrics = [
        ZoneDwellMetric(
            zone_id=r["zone_id"],
            avg_dwell_ms=round(float(r["avg_dwell_ms"] or 0), 2),
            visit_count=int(r["visit_count"] or 0),
        )
        for r in zone_rows
    ]

    # ── Current queue depth (most recent BILLING_QUEUE_JOIN) ──────────────────
    q_row = conn.execute(
        """
        SELECT queue_depth
        FROM events
        WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (store_id,),
    ).fetchone()
    current_queue_depth: int = int(q_row["queue_depth"] or 0) if q_row else 0

    # ── Abandonment rate ──────────────────────────────────────────────────────
    ab = conn.execute(
        """
        SELECT
          SUM(reached_billing)   AS reached,
          SUM(abandoned_billing) AS abandoned
        FROM visitor_sessions
        WHERE store_id = ? AND date = ? AND is_staff = 0
        """,
        (store_id, date),
    ).fetchone()

    if ab and ab["reached"] and ab["reached"] > 0:
        abandonment_rate = float(ab["abandoned"] or 0) / ab["reached"]
    else:
        abandonment_rate = 0.0

    return MetricsResponse(
        store_id=store_id,
        date=date,
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_per_zone=dwell_metrics,
        current_queue_depth=current_queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
    )
