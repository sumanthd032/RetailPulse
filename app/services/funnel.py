"""Conversion funnel — session is the unit, REENTRY does not create new sessions."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..db import get_db
from ..models import FunnelResponse, FunnelStage

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def compute_funnel(store_id: str, date: Optional[str] = None) -> FunnelResponse:
    """Compute conversion funnel with per-stage drop-off percentages.

    Funnel stages:
      Entry → Zone Visit → Billing Queue → Purchase

    The visitor_sessions table tracks boolean flags for each stage.
    A REENTRY visitor counts once (their session already exists).
    """
    if date is None:
        date = _today()

    conn = get_db()

    row = conn.execute(
        """
        SELECT
          COUNT(*)                   AS entry_count,
          SUM(reached_zone)          AS zone_count,
          SUM(reached_billing)       AS billing_count,
          SUM(converted)             AS purchase_count
        FROM visitor_sessions
        WHERE store_id = ? AND date = ? AND is_staff = 0
        """,
        (store_id, date),
    ).fetchone()

    if row is None:
        counts = [0, 0, 0, 0]
    else:
        counts = [
            int(row["entry_count"]   or 0),
            int(row["zone_count"]    or 0),
            int(row["billing_count"] or 0),
            int(row["purchase_count"] or 0),
        ]

    stage_names = ["Entry", "Zone Visit", "Billing Queue", "Purchase"]
    stages: list[FunnelStage] = []

    for i, (name, count) in enumerate(zip(stage_names, counts)):
        prev = counts[i - 1] if i > 0 else count
        drop_off_pct = (
            round((prev - count) / prev * 100, 1)
            if prev > 0 and i > 0
            else 0.0
        )
        stages.append(FunnelStage(stage=name, count=count, drop_off_pct=drop_off_pct))

    return FunnelResponse(store_id=store_id, date=date, stages=stages)
