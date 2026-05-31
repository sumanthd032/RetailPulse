"""Zone heatmap — visit frequency and avg dwell, normalised 0–100."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..db import get_db
from ..models import HeatmapResponse, HeatmapZone
from .utils import effective_date

logger = logging.getLogger(__name__)

DATA_CONFIDENCE_MIN_SESSIONS = 20


def _normalise(values: list[float]) -> list[float]:
    """Min-max normalise a list to [0, 100]. Returns zeros if all values equal."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [50.0] * len(values)
    return [round((v - lo) / (hi - lo) * 100, 1) for v in values]


def compute_heatmap(store_id: str, date: Optional[str] = None) -> HeatmapResponse:
    """Compute per-zone visit frequency and dwell scores normalised 0–100.

    Data confidence is flagged False when fewer than DATA_CONFIDENCE_MIN_SESSIONS
    sessions exist for the date — the heatmap on sparse data is noisy.
    """
    conn = get_db()
    date = effective_date(store_id, conn, date)

    # Session count for confidence flag
    sess_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM visitor_sessions WHERE store_id = ? AND date = ? AND is_staff = 0",
        (store_id, date),
    ).fetchone()
    session_count = int(sess_row["cnt"] or 0) if sess_row else 0
    overall_confidence = session_count >= DATA_CONFIDENCE_MIN_SESSIONS

    # Per-zone metrics:
    # - visit_count from ZONE_ENTER events (always present, one per visit)
    # - avg_dwell_ms from ZONE_EXIT events (dwell_ms is populated on exit) with
    #   ZONE_DWELL as fallback when no exit was seen yet
    rows = conn.execute(
        """
        SELECT
          enters.zone_id                              AS zone_id,
          enters.visits                               AS visit_count,
          COALESCE(exits.avg_dwell, dwells.avg_dwell, 0) AS avg_dwell_ms
        FROM (
          SELECT zone_id, COUNT(DISTINCT visitor_id) AS visits
          FROM events
          WHERE store_id       = ?
            AND event_type     = 'ZONE_ENTER'
            AND is_staff       = 0
            AND date(timestamp) = ?
            AND zone_id IS NOT NULL
          GROUP BY zone_id
        ) AS enters
        LEFT JOIN (
          SELECT zone_id, AVG(dwell_ms) AS avg_dwell
          FROM events
          WHERE store_id       = ?
            AND event_type     = 'ZONE_EXIT'
            AND is_staff       = 0
            AND date(timestamp) = ?
            AND zone_id IS NOT NULL
            AND dwell_ms > 0
          GROUP BY zone_id
        ) AS exits ON enters.zone_id = exits.zone_id
        LEFT JOIN (
          SELECT zone_id, AVG(max_dwell) AS avg_dwell
          FROM (
            SELECT zone_id, visitor_id, MAX(dwell_ms) AS max_dwell
            FROM events
            WHERE store_id       = ?
              AND event_type     = 'ZONE_DWELL'
              AND is_staff       = 0
              AND date(timestamp) = ?
              AND zone_id IS NOT NULL
            GROUP BY zone_id, visitor_id
          )
          GROUP BY zone_id
        ) AS dwells ON enters.zone_id = dwells.zone_id
        ORDER BY enters.visits DESC
        """,
        (store_id, date, store_id, date, store_id, date),
    ).fetchall()

    if not rows:
        return HeatmapResponse(
            store_id=store_id,
            zones=[],
            data_confidence=overall_confidence,
        )

    zone_ids       = [r["zone_id"]                    for r in rows]
    visit_counts   = [float(r["visit_count"]  or 0)   for r in rows]
    avg_dwells     = [float(r["avg_dwell_ms"] or 0)   for r in rows]

    visit_scores = _normalise(visit_counts)
    dwell_scores = _normalise(avg_dwells)

    zones = [
        HeatmapZone(
            zone_id=zone_ids[i],
            visit_score=visit_scores[i],
            dwell_score=dwell_scores[i],
            raw_visit_count=int(visit_counts[i]),
            raw_avg_dwell_ms=round(avg_dwells[i], 2),
            data_confidence=overall_confidence,
        )
        for i in range(len(zone_ids))
    ]

    return HeatmapResponse(
        store_id=store_id,
        zones=zones,
        data_confidence=overall_confidence,
    )
