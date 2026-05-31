from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class StoreEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: str  # ISO-8601 UTC
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("zone_id")
    @classmethod
    def zone_null_for_entry_exit(cls, v: Optional[str], info: Any) -> Optional[str]:
        if info.data.get("event_type") in (EventType.ENTRY, EventType.EXIT, EventType.REENTRY):
            return None
        return v

    @field_validator("dwell_ms")
    @classmethod
    def dwell_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("dwell_ms must be non-negative")
        return v

    model_config = {"use_enum_values": True}


# ── API request/response models ──────────────────────────────────────────────

class IngestRequest(BaseModel):
    events: list[StoreEvent] = Field(..., max_length=500)


class IngestError(BaseModel):
    event_id: str
    reason: str


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    errors: list[IngestError] = []


class ZoneDwellMetric(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class MetricsResponse(BaseModel):
    store_id: str
    date: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: list[ZoneDwellMetric]
    current_queue_depth: int
    abandonment_rate: float


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    date: str
    stages: list[FunnelStage]


class HeatmapZone(BaseModel):
    zone_id: str
    visit_score: float  # 0–100
    dwell_score: float  # 0–100
    raw_visit_count: int
    raw_avg_dwell_ms: float
    data_confidence: bool = True


class HeatmapResponse(BaseModel):
    store_id: str
    zones: list[HeatmapZone]
    data_confidence: bool


class Anomaly(BaseModel):
    anomaly_type: str
    severity: str  # INFO / WARN / CRITICAL
    zone_id: Optional[str] = None
    current_value: Optional[float] = None
    baseline_value: Optional[float] = None
    suggested_action: str
    detected_at: str
    data_confidence: bool = True


class AnomalyResponse(BaseModel):
    store_id: str
    anomalies: list[Anomaly]


class StoreHealthInfo(BaseModel):
    last_event_at: Optional[str]
    feed_status: str  # OK / STALE_FEED / NO_DATA


class HealthResponse(BaseModel):
    status: str  # healthy / degraded
    db: str  # connected / disconnected
    stores: dict[str, StoreHealthInfo]
    uptime_seconds: float
