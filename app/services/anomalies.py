"""Anomaly detection — three detectors with severity levels and bootstrap handling.

BILLING_QUEUE_SPIKE   current queue depth vs same-hour rolling average (7-day)
CONVERSION_DROP       today's rate vs 7-day same-weekday average
DEAD_ZONE             no ZONE_ENTER events for any zone in the last 30 minutes
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..db import get_db
from ..models import Anomaly, AnomalyResponse

logger = logging.getLogger(__name__)

STALE_ZONE_MINUTES = 30
STORE_OPEN_HOUR    = 10  # 10:00 UTC (~15:30 IST open)
STORE_CLOSE_HOUR   = 15  # 15:30 UTC (~21:00 IST close)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_str() -> str:
    return _now_utc().strftime("%Y-%m-%d")


def _iso_now() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


# ── BILLING_QUEUE_SPIKE ───────────────────────────────────────────────────────

def _detect_queue_spike(store_id: str, conn) -> Optional[Anomaly]:
    # Current queue depth (most recent BILLING_QUEUE_JOIN event)
    row = conn.execute(
        """
        SELECT queue_depth, timestamp
        FROM events
        WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
        ORDER BY timestamp DESC LIMIT 1
        """,
        (store_id,),
    ).fetchone()

    if row is None or row["queue_depth"] is None:
        return None

    current_depth = float(row["queue_depth"])
    current_hour  = _now_utc().strftime("%H")

    # 7-day rolling average for the same hour
    hist = conn.execute(
        """
        SELECT AVG(queue_depth) AS avg_depth, COUNT(DISTINCT date(timestamp)) AS day_count
        FROM events
        WHERE store_id    = ?
          AND event_type  = 'BILLING_QUEUE_JOIN'
          AND strftime('%H', timestamp) = ?
          AND date(timestamp) < date('now')
          AND date(timestamp) >= date('now', '-7 days')
        """,
        (store_id, current_hour),
    ).fetchone()

    day_count   = int(hist["day_count"] or 0) if hist else 0
    has_history = day_count >= 2
    baseline    = float(hist["avg_depth"] or 0) if hist and hist["avg_depth"] else 0.0

    # Bootstrap: no history → use absolute threshold
    if not has_history:
        if current_depth >= 8:
            return Anomaly(
                anomaly_type="BILLING_QUEUE_SPIKE",
                severity="CRITICAL",
                current_value=current_depth,
                baseline_value=None,
                suggested_action="Open additional billing counter immediately",
                detected_at=_iso_now(),
                data_confidence=False,
            )
        if current_depth >= 5:
            return Anomaly(
                anomaly_type="BILLING_QUEUE_SPIKE",
                severity="WARN",
                current_value=current_depth,
                baseline_value=None,
                suggested_action="Monitor queue — consider calling backup cashier",
                detected_at=_iso_now(),
                data_confidence=False,
            )
        return None

    if baseline == 0:
        return None

    ratio = current_depth / baseline

    if ratio >= 2.5:
        severity = "CRITICAL"
        action   = "Open additional billing counter immediately"
    elif ratio >= 1.5:
        severity = "WARN"
        action   = "Monitor queue closely — consider calling backup cashier"
    else:
        return None  # normal

    return Anomaly(
        anomaly_type="BILLING_QUEUE_SPIKE",
        severity=severity,
        current_value=current_depth,
        baseline_value=round(baseline, 2),
        suggested_action=action,
        detected_at=_iso_now(),
        data_confidence=True,
    )


# ── CONVERSION_DROP ──────────────────────────────────────────────────────────

def _detect_conversion_drop(store_id: str, conn) -> Optional[Anomaly]:
    today = _today_str()
    now   = _now_utc()

    # Only trigger if store has been open for at least 2 hours
    if now.hour < STORE_OPEN_HOUR + 2:
        return None

    # Today's conversion rate
    today_row = conn.execute(
        """
        SELECT
          COUNT(*)       AS total,
          SUM(converted) AS conv
        FROM visitor_sessions
        WHERE store_id = ? AND date = ? AND is_staff = 0
        """,
        (store_id, today),
    ).fetchone()

    if not today_row or not today_row["total"] or today_row["total"] < 5:
        return None  # too few visitors for meaningful comparison

    today_rate = float(today_row["conv"] or 0) / today_row["total"]

    # 7-day same-weekday rolling average
    hist = conn.execute(
        """
        SELECT AVG(daily_rate) AS avg_rate, COUNT(*) AS day_count
        FROM (
          SELECT date,
                 CAST(SUM(converted) AS REAL) / NULLIF(COUNT(*), 0) AS daily_rate
          FROM visitor_sessions
          WHERE store_id = ?
            AND is_staff = 0
            AND date < ?
            AND date >= date(?, '-7 days')
          GROUP BY date
          HAVING COUNT(*) >= 5
        )
        """,
        (store_id, today, today),
    ).fetchone()

    day_count   = int(hist["day_count"] or 0) if hist else 0
    has_history = day_count >= 2
    baseline    = float(hist["avg_rate"] or 0) if hist and hist["avg_rate"] else None

    if not has_history or baseline is None or baseline == 0:
        return None  # not enough history to detect a drop

    drop_pct = (baseline - today_rate) / baseline

    if drop_pct >= 0.40:
        severity = "CRITICAL"
        action   = "Review pricing, staff availability, and product stocking immediately"
    elif drop_pct >= 0.20:
        severity = "WARN"
        action   = "Investigate conversion barriers — check queue wait times and product availability"
    elif drop_pct >= 0.10:
        severity = "INFO"
        action   = "Monitor — conversion is slightly below trend"
    else:
        return None

    return Anomaly(
        anomaly_type="CONVERSION_DROP",
        severity=severity,
        current_value=round(today_rate * 100, 2),
        baseline_value=round(baseline * 100, 2),
        suggested_action=action,
        detected_at=_iso_now(),
        data_confidence=True,
    )


# ── DEAD_ZONE ─────────────────────────────────────────────────────────────────

def _detect_dead_zones(store_id: str, conn) -> list[Anomaly]:
    now      = _now_utc()
    today    = _today_str()

    # Only fire during store open hours
    if not (STORE_OPEN_HOUR <= now.hour < STORE_CLOSE_HOUR):
        return []

    cutoff = (now - timedelta(minutes=STALE_ZONE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Zones that have had at least one ZONE_ENTER today
    active_zones = conn.execute(
        """
        SELECT zone_id, MAX(timestamp) AS last_seen
        FROM events
        WHERE store_id   = ?
          AND event_type = 'ZONE_ENTER'
          AND is_staff   = 0
          AND date(timestamp) = ?
          AND zone_id IS NOT NULL
        GROUP BY zone_id
        """,
        (store_id, today),
    ).fetchall()

    anomalies: list[Anomaly] = []
    for row in active_zones:
        if row["last_seen"] < cutoff:
            anomalies.append(
                Anomaly(
                    anomaly_type="DEAD_ZONE",
                    severity="INFO",
                    zone_id=row["zone_id"],
                    current_value=None,
                    baseline_value=None,
                    suggested_action=(
                        f"Inspect zone '{row['zone_id']}' — "
                        "no customer visits in past 30 minutes. "
                        "Check display and camera feed."
                    ),
                    detected_at=_iso_now(),
                    data_confidence=True,
                )
            )

    return anomalies


# ── Public API ────────────────────────────────────────────────────────────────

def compute_anomalies(store_id: str) -> AnomalyResponse:
    conn = get_db()
    anomalies: list[Anomaly] = []

    try:
        q = _detect_queue_spike(store_id, conn)
        if q:
            anomalies.append(q)
    except Exception as exc:
        logger.error("Queue spike detection failed for %s: %s", store_id, exc)

    try:
        c = _detect_conversion_drop(store_id, conn)
        if c:
            anomalies.append(c)
    except Exception as exc:
        logger.error("Conversion drop detection failed for %s: %s", store_id, exc)

    try:
        dz = _detect_dead_zones(store_id, conn)
        anomalies.extend(dz)
    except Exception as exc:
        logger.error("Dead zone detection failed for %s: %s", store_id, exc)

    # Sort: CRITICAL first, then WARN, then INFO
    order = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
    anomalies.sort(key=lambda a: order.get(a.severity, 9))

    return AnomalyResponse(store_id=store_id, anomalies=anomalies)
