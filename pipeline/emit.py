"""Event emitter — builds StoreEvents, validates via Pydantic, writes JSONL.

Every event passes through Pydantic validation before being written. Schema
violations are logged and dropped — never silently corrupted.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.models import EventMetadata, EventType, StoreEvent

logger = logging.getLogger(__name__)


class EventEmitter:
    """Thread-unsafe single-process event writer."""

    def __init__(self, output_path: str) -> None:
        self._path = Path(output_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")
        self._written = 0
        self._rejected = 0

    def _write(self, event: StoreEvent) -> None:
        try:
            line = event.model_dump_json()
            self._fh.write(line + "\n")
            self._written += 1
        except Exception as exc:
            logger.error("Failed to write event %s: %s", event.event_id, exc)
            self._rejected += 1

    def _make(
        self,
        *,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        event_type: EventType,
        timestamp: str,
        zone_id: Optional[str],
        dwell_ms: int,
        is_staff: bool,
        confidence: float,
        session_seq: int,
        queue_depth: Optional[int] = None,
        sku_zone: Optional[str] = None,
    ) -> Optional[StoreEvent]:
        try:
            ev = StoreEvent(
                event_id=str(uuid.uuid4()),
                store_id=store_id,
                camera_id=camera_id,
                visitor_id=visitor_id,
                event_type=event_type,
                timestamp=timestamp,
                zone_id=zone_id,
                dwell_ms=dwell_ms,
                is_staff=is_staff,
                confidence=round(confidence, 4),
                metadata=EventMetadata(
                    queue_depth=queue_depth,
                    sku_zone=sku_zone,
                    session_seq=session_seq,
                ),
            )
            return ev
        except Exception as exc:
            logger.error("Event validation failed (%s %s): %s", event_type, visitor_id, exc)
            self._rejected += 1
            return None

    # ── Public emit methods ───────────────────────────────────────────────────

    def emit_entry(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        confidence: float,
        is_staff: bool,
        session_seq: int,
    ) -> None:
        ev = self._make(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=EventType.ENTRY,
            timestamp=timestamp,
            zone_id=None,
            dwell_ms=0,
            is_staff=is_staff,
            confidence=confidence,
            session_seq=session_seq,
        )
        if ev:
            self._write(ev)

    def emit_exit(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        confidence: float,
        is_staff: bool,
        session_seq: int,
    ) -> None:
        ev = self._make(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=EventType.EXIT,
            timestamp=timestamp,
            zone_id=None,
            dwell_ms=0,
            is_staff=is_staff,
            confidence=confidence,
            session_seq=session_seq,
        )
        if ev:
            self._write(ev)

    def emit_reentry(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        confidence: float,
        is_staff: bool,
        session_seq: int,
    ) -> None:
        ev = self._make(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=EventType.REENTRY,
            timestamp=timestamp,
            zone_id=None,
            dwell_ms=0,
            is_staff=is_staff,
            confidence=confidence,
            session_seq=session_seq,
        )
        if ev:
            self._write(ev)

    def emit_zone_enter(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        zone_id: str,
        confidence: float,
        is_staff: bool,
        session_seq: int,
        sku_zone: Optional[str] = None,
    ) -> None:
        ev = self._make(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=EventType.ZONE_ENTER,
            timestamp=timestamp,
            zone_id=zone_id,
            dwell_ms=0,
            is_staff=is_staff,
            confidence=confidence,
            session_seq=session_seq,
            sku_zone=sku_zone,
        )
        if ev:
            self._write(ev)

    def emit_zone_exit(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        zone_id: str,
        dwell_ms: int,
        confidence: float,
        is_staff: bool,
        session_seq: int,
        sku_zone: Optional[str] = None,
    ) -> None:
        ev = self._make(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=EventType.ZONE_EXIT,
            timestamp=timestamp,
            zone_id=zone_id,
            dwell_ms=dwell_ms,
            is_staff=is_staff,
            confidence=confidence,
            session_seq=session_seq,
            sku_zone=sku_zone,
        )
        if ev:
            self._write(ev)

    def emit_zone_dwell(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        zone_id: str,
        dwell_ms: int,
        confidence: float,
        is_staff: bool,
        session_seq: int,
        sku_zone: Optional[str] = None,
    ) -> None:
        ev = self._make(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=EventType.ZONE_DWELL,
            timestamp=timestamp,
            zone_id=zone_id,
            dwell_ms=dwell_ms,
            is_staff=is_staff,
            confidence=confidence,
            session_seq=session_seq,
            sku_zone=sku_zone,
        )
        if ev:
            self._write(ev)

    def emit_billing_queue_join(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        queue_depth: int,
        confidence: float,
        is_staff: bool,
        session_seq: int,
    ) -> None:
        ev = self._make(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=EventType.BILLING_QUEUE_JOIN,
            timestamp=timestamp,
            zone_id="BILLING_QUEUE",
            dwell_ms=0,
            is_staff=is_staff,
            confidence=confidence,
            session_seq=session_seq,
            queue_depth=queue_depth,
        )
        if ev:
            self._write(ev)

    def emit_billing_queue_abandon(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        dwell_ms: int,
        confidence: float,
        is_staff: bool,
        session_seq: int,
    ) -> None:
        ev = self._make(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=EventType.BILLING_QUEUE_ABANDON,
            timestamp=timestamp,
            zone_id="BILLING_QUEUE",
            dwell_ms=dwell_ms,
            is_staff=is_staff,
            confidence=confidence,
            session_seq=session_seq,
        )
        if ev:
            self._write(ev)

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> tuple[int, int]:
        self._fh.flush()
        self._fh.close()
        return self._written, self._rejected

    @property
    def stats(self) -> dict:
        return {"written": self._written, "rejected": self._rejected}
