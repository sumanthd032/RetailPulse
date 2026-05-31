from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class PipelineConfig:
    # ── Detection ──────────────────────────────────────────────────────────
    yolo_model: str = "yolov8n.pt"  # nano for speed; swap to yolov9c.pt for best accuracy
    detection_confidence: float = 0.35   # lower threshold — emit with confidence field
    nms_iou: float = 0.45
    imgsz: int = 640
    device: str = ""                      # "" = auto (CUDA if available, else CPU)

    # ── Tracking (ByteTrack) ────────────────────────────────────────────────
    tracker_config: str = "pipeline/bytetrack.yaml"
    frame_skip: int = 3                   # process every Nth frame (5fps effective from 15fps)

    # ── Re-ID Gallery ────────────────────────────────────────────────────────
    # Threshold tuned empirically on real footage. At 0.72 too many distinct
    # visitors collapse to the same visitor_id (false REENTRYs). At 0.85 the
    # gallery only matches visually very similar people — fewer false positives
    # at the cost of missing some genuine re-entries when clothing changes.
    # For retail CCTV with stable lighting and short sessions, 0.85 is conservative.
    reid_similarity_threshold: float = 0.85
    reid_gallery_ttl_seconds: int = 300        # 5 minutes — gallery entry expiry
    reid_min_stable_frames: int = 8            # frames before gallery write (was 5)
    reid_camera_handoff_window_s: int = 30     # tighter window for same-frame handoff

    # ── Staff Detection ──────────────────────────────────────────────────────
    staff_trajectory_threshold: float = 0.60  # fraction of time in staff zones
    staff_trajectory_min_duration_s: int = 300  # 5 min before trajectory signal fires
    staff_color_tolerance: int = 20           # HSV tolerance around reference color

    # ── Zone Events ──────────────────────────────────────────────────────────
    zone_dwell_interval_s: int = 30           # emit ZONE_DWELL every N seconds

    # ── Entry / Re-entry ─────────────────────────────────────────────────────
    reentry_window_s: int = 300              # 5 minutes max gap for re-entry
    group_entry_window_s: float = 2.0       # tracks crossing entry line within this window = group
    entry_line_crossing_frames: int = 3     # frames centroid must cross line (debounce)

    # ── Billing ──────────────────────────────────────────────────────────────
    billing_abandon_window_s: int = 300     # 5 min after billing exit — check for POS

    # ── Store / clip ──────────────────────────────────────────────────────────
    store_id: str = "STORE_BLR_002"
    store_layout_path: str = "data/store_layout.json"
    clips_config_path: str = "data/clips_config.json"
    pos_csv_path: str = "data/pos_transactions.csv"

    # ── Output ───────────────────────────────────────────────────────────────
    output_path: str = "data/events.jsonl"

    @classmethod
    def from_env(cls) -> "PipelineConfig":
        cfg = cls()
        cfg.yolo_model        = os.getenv("YOLO_MODEL", cfg.yolo_model)
        cfg.detection_confidence = float(os.getenv("DETECTION_CONF", cfg.detection_confidence))
        cfg.frame_skip        = int(os.getenv("FRAME_SKIP", cfg.frame_skip))
        cfg.store_id          = os.getenv("STORE_ID", cfg.store_id)
        cfg.output_path       = os.getenv("OUTPUT_PATH", cfg.output_path)
        cfg.pos_csv_path      = os.getenv("POS_CSV", cfg.pos_csv_path)
        cfg.device            = os.getenv("DEVICE", cfg.device)
        return cfg
