"""SQLite database setup and connection management.

Uses WAL mode for concurrent reads while writing.
Schema is created on first startup — no separate migration step needed.
"""

from __future__ import annotations

import csv
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "data/retail.db")
POS_CSV = os.getenv("POS_CSV", "data/pos_transactions.csv")

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA cache_size   = -65536;   -- 64 MB
PRAGMA temp_store   = MEMORY;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT    PRIMARY KEY,
    store_id        TEXT    NOT NULL,
    camera_id       TEXT    NOT NULL,
    visitor_id      TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    zone_id         TEXT,
    dwell_ms        INTEGER NOT NULL DEFAULT 0,
    is_staff        INTEGER NOT NULL DEFAULT 0,
    confidence      REAL    NOT NULL,
    queue_depth     INTEGER,
    sku_zone        TEXT,
    session_seq     INTEGER,
    ingested_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ev_store_time  ON events (store_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_ev_visitor     ON events (visitor_id, store_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_ev_type_store  ON events (store_id, event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_ev_zone_store  ON events (store_id, zone_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_ev_staff       ON events (store_id, is_staff, event_type);

CREATE TABLE IF NOT EXISTS visitor_sessions (
    id                  TEXT    PRIMARY KEY,  -- store_id||'_'||visitor_id||'_'||date
    store_id            TEXT    NOT NULL,
    visitor_id          TEXT    NOT NULL,
    date                TEXT    NOT NULL,     -- YYYY-MM-DD
    is_staff            INTEGER NOT NULL DEFAULT 0,
    first_entry_at      TEXT    NOT NULL,
    last_event_at       TEXT    NOT NULL,
    reached_zone        INTEGER NOT NULL DEFAULT 0,
    reached_billing     INTEGER NOT NULL DEFAULT 0,
    converted           INTEGER NOT NULL DEFAULT 0,
    abandoned_billing   INTEGER NOT NULL DEFAULT 0,
    reentry_count       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sess_store_date ON visitor_sessions (store_id, date);
CREATE INDEX IF NOT EXISTS idx_sess_staff       ON visitor_sessions (store_id, is_staff, date);

CREATE TABLE IF NOT EXISTS pos_transactions (
    transaction_id      TEXT    PRIMARY KEY,
    store_id            TEXT    NOT NULL,
    timestamp           TEXT    NOT NULL,
    basket_value_inr    REAL    NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_pos_store_time ON pos_transactions (store_id, timestamp);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    store_id        TEXT    NOT NULL,
    date            TEXT    NOT NULL,
    unique_visitors INTEGER NOT NULL DEFAULT 0,
    converted       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (store_id, date)
);
"""


def _get_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")
    conn.execute("PRAGMA cache_size   = -65536")
    conn.execute("PRAGMA temp_store   = MEMORY")
    return conn


# Module-level connection (single writer, multiple readers via WAL)
_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _conn


@contextmanager
def db_cursor() -> Generator[sqlite3.Cursor, None, None]:
    conn = get_db()
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def init_db() -> None:
    global _conn
    _conn = _get_connection()
    _conn.executescript(SCHEMA_SQL)
    _conn.commit()
    logger.info("Database initialised at %s", DB_PATH)
    _load_pos_if_needed()


def _load_pos_if_needed() -> None:
    """Load POS transactions from CSV on first startup (idempotent)."""
    csv_path = Path(POS_CSV)
    if not csv_path.exists():
        logger.info("No POS CSV found at %s — skipping", POS_CSV)
        return

    conn = get_db()
    existing = conn.execute("SELECT COUNT(*) FROM pos_transactions").fetchone()[0]
    if existing > 0:
        logger.info("POS transactions already loaded (%d rows)", existing)
        return

    rows_loaded = 0
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO pos_transactions "
                    "(transaction_id, store_id, timestamp, basket_value_inr) VALUES (?,?,?,?)",
                    (
                        row["transaction_id"],
                        row["store_id"],
                        row["timestamp"],
                        float(row.get("basket_value_inr", 0)),
                    ),
                )
                rows_loaded += 1
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping POS row: %s", exc)

    conn.commit()
    logger.info("Loaded %d POS transactions", rows_loaded)


def close_db() -> None:
    global _conn
    if _conn:
        _conn.close()
        _conn = None
