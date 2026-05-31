"""Shared service utilities."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional


def effective_date(store_id: str, conn: sqlite3.Connection, requested: Optional[str] = None) -> str:
    """Return the requested date if given, else the most recent date that has data.

    This means the API always returns real data regardless of whether the events
    were ingested today or a month ago — it finds whatever date has events and
    uses that. If date is explicitly requested (e.g., from query param or dashboard
    date picker), honour it.
    """
    if requested:
        return requested

    row = conn.execute(
        "SELECT MAX(date(timestamp)) AS d FROM events WHERE store_id = ? AND is_staff = 0",
        (store_id,),
    ).fetchone()

    if row and row["d"]:
        return row["d"]

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
