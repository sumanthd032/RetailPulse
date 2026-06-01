"""Main detection pipeline — frame-by-frame processing.

Architecture:
  Frame → YOLOv9c (person detection) → ByteTrack (multi-object tracking)
       → Re-ID gallery (visitor_id persistence)
       → Zone classifier (point-in-polygon)
       → Staff classifier (colour + trajectory)
       → State machine (event generation)
       → EventEmitter (JSONL output)
"""

from __future__ import annotations

import csv
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .config import PipelineConfig
from .emit import EventEmitter
from .staff import StaffClassifier
from .tracker import ReIDGallery, TrackState
from .zones import ZoneManager

logger = logging.getLogger(__name__)

_BILLING_ZONES = {"BILLING_QUEUE", "CASH_COUNTER"}
_INVISIBLE_ZONE_TYPES = {"entry"}  # cameras where we only track crossings, no zone events


def _ts_from_frame(clip_start: datetime, frame_idx: int, fps: float) -> str:
    """Derive ISO-8601 UTC timestamp from clip start + frame offset."""
    offset = timedelta(seconds=frame_idx / fps)
    return (clip_start + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_pos_transactions(csv_path: str) -> list[dict]:
    """Load POS transactions for billing abandonment correlation."""
    path = Path(csv_path)
    if not path.exists():
        logger.warning("pos_transactions.csv not found at %s", csv_path)
        return []
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _has_pos_within_window(
    pos_records: list[dict],
    store_id: str,
    after_ts: str,
    window_s: int = 300,
) -> bool:
    """Return True if a POS transaction exists within the window after after_ts."""
    try:
        t0 = datetime.strptime(after_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False

    for rec in pos_records:
        if rec.get("store_id") != store_id:
            continue
        try:
            t_pos = datetime.strptime(rec["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, KeyError):
            continue
        delta = (t_pos - t0).total_seconds()
        if 0 <= delta <= window_s:
            return True
    return False


class VisitorStateManager:
    """Tracks per-visitor state for event generation.

    This is the central state machine. On each frame it receives the current
    set of active tracks and generates the appropriate events.
    """

    def __init__(
        self,
        zone_manager: ZoneManager,
        reid_gallery: ReIDGallery,
        staff_classifier: StaffClassifier,
        emitter: EventEmitter,
        config: PipelineConfig,
        camera_id: str,
        camera_type: str,
        pos_records: list[dict],
    ) -> None:
        self._zones = zone_manager
        self._gallery = reid_gallery
        self._staff = staff_classifier
        self._emitter = emitter
        self._cfg = config
        self._camera_id = camera_id
        self._camera_type = camera_type
        self._pos_records = pos_records

        # Active track states: track_id → TrackState
        self._active: dict[int, TrackState] = {}

        # Tracks seen this frame
        self._seen_this_frame: set[int] = set()

        # Billing zone: current visitors for queue depth
        self._in_billing_now: set[str] = set()  # visitor_ids

        # Pending billing exits for abandonment check: visitor_id → (exit_ts, enter_ts, dwell_ms, conf, is_staff, seq)
        self._pending_billing_exits: dict[str, tuple] = {}

        # Calibration frame index
        self._frame_idx = 0

    @property
    def _store_id(self) -> str:
        return self._cfg.store_id

    def _frame_ts(self, clip_start: datetime, frame_idx: int, fps: float) -> str:
        return _ts_from_frame(clip_start, frame_idx, fps)

    def process_frame(
        self,
        frame: np.ndarray,
        track_ids: np.ndarray,
        bboxes: np.ndarray,
        confidences: np.ndarray,
        frame_idx: int,
        fps: float,
        clip_start: datetime,
    ) -> None:
        """Process one frame of tracked detections."""
        self._frame_idx = frame_idx
        frame_ts = self._frame_ts(clip_start, frame_idx, fps)
        # Footage time (epoch seconds) for this frame. Dwell timing and Re-ID
        # recency are measured in this store-clock time, not processing wall-clock,
        # so durations are real and simultaneous cameras can be linked.
        frame_epoch = clip_start.timestamp() + frame_idx / fps
        h, w = frame.shape[:2]
        self._seen_this_frame.clear()

        # Pass detections to staff calibrator (billing zone context)
        if len(track_ids) > 0:
            calib_input = []
            for i, (tid, bbox) in enumerate(zip(track_ids, bboxes)):
                zone = self._zones.get_zone(bbox, frame.shape, self._camera_id)
                calib_input.append((bbox, zone or ""))
            self._staff.calibrate_from_frame(frame, calib_input)

        for i, track_id in enumerate(track_ids):
            track_id = int(track_id)
            bbox = bboxes[i]
            conf = float(confidences[i])
            self._seen_this_frame.add(track_id)

            # Zone classification
            current_zone = self._zones.get_zone(bbox, frame.shape, self._camera_id)

            # Foot position for entry-line checks
            x1, y1, x2, y2 = bbox[:4]
            foot = ((x1 + x2) / 2.0, float(y2))

            # Staff classification
            is_staff_zone = (
                self._zones.is_staff_zone(self._camera_id, current_zone)
                if current_zone
                else False
            )
            is_staff, staff_conf = self._staff.update(
                track_id, frame, bbox, current_zone, is_staff_zone
            )

            if track_id not in self._active:
                # New track appeared — Re-ID check
                visitor_id, is_reentry, is_brand_new = self._gallery.register_or_reidentify(
                    track_id=track_id,
                    frame=frame,
                    bbox=bbox,
                    camera_id=self._camera_id,
                    camera_type=self._camera_type,
                    now_ts=frame_epoch,
                    reentry_window_s=self._cfg.reentry_window_s,
                )
                state = TrackState(
                    track_id=track_id,
                    visitor_id=visitor_id,
                    store_id=self._store_id,
                    camera_id=self._camera_id,
                    is_staff=is_staff,
                    prev_foot=foot,
                )
                self._active[track_id] = state

                if is_reentry and self._camera_type == "entry":
                    seq = state.next_seq()
                    self._emitter.emit_reentry(
                        self._store_id, self._camera_id, visitor_id,
                        frame_ts, conf, is_staff, seq,
                    )
                    state.has_entered = True

                elif is_brand_new and self._camera_type == "entry":
                    # Check if person is already on the INSIDE of the entry threshold.
                    # This happens when the clip starts with people already in the store,
                    # or when the tracker stabilises several frames after someone entered.
                    # In both cases we emit ENTRY immediately rather than waiting for a
                    # threshold crossing that will never come.
                    line = self._zones.get_entry_line(self._camera_id, w, h)
                    already_inside = False
                    if line is not None:
                        lx1, ly1, lx2, ly2 = line
                        cam_cfg = self._zones._cameras.get(self._camera_id)
                        inbound = cam_cfg.inbound_side if cam_cfg else "right"
                        is_horiz = abs(ly2 - ly1) < 2  # horizontal line (y1≈y2)
                        if is_horiz:
                            already_inside = (foot[1] < ly1) if inbound == "above" else (foot[1] > ly1)
                        else:
                            already_inside = (foot[0] > lx1) if inbound == "right" else (foot[0] < lx1)

                    if already_inside:
                        seq = state.next_seq()
                        self._emitter.emit_entry(
                            self._store_id, self._camera_id, visitor_id,
                            frame_ts, conf, is_staff, seq,
                        )
                        state.has_entered = True
                    # else: wait for explicit threshold crossing (handled below)

                # For floor/billing cameras: no ENTRY event — camera handoff
            else:
                state = self._active[track_id]
                state.is_staff = is_staff

            # ── Entry line crossing detection ─────────────────────────────────
            if self._camera_type == "entry" and state.prev_foot is not None:
                direction = self._zones.check_line_crossing(
                    state.prev_foot, foot,
                    self._camera_id, w, h,
                )
                if direction == "inbound" and not state.has_entered:
                    seq = state.next_seq()
                    self._emitter.emit_entry(
                        self._store_id, self._camera_id, state.visitor_id,
                        frame_ts, conf, state.is_staff, seq,
                    )
                    state.has_entered = True
                elif direction == "outbound" and state.has_entered:
                    seq = state.next_seq()
                    self._emitter.emit_exit(
                        self._store_id, self._camera_id, state.visitor_id,
                        frame_ts, conf, state.is_staff, seq,
                    )
                    state.has_entered = False

            state.prev_foot = foot

            # ── Zone state transitions ─────────────────────────────────────────
            if self._camera_type != "entry":  # floor and billing cameras track zones
                self._update_zone_state(state, current_zone, frame_ts, conf, frame_epoch)

            # ── Billing queue logic (billing camera only) ─────────────────────
            if self._camera_type == "billing":
                self._update_billing_state(state, current_zone, frame_ts, conf, frame_epoch)

            # Refresh gallery embedding periodically
            if frame_idx % 15 == 0:
                self._gallery.update_gallery(track_id, frame, bbox, self._camera_id, frame_epoch, is_staff)

        # ── Handle disappeared tracks ─────────────────────────────────────────
        self._handle_disappeared(frame_ts, frame_epoch)

        # ── Check pending billing exits for POS correlation ───────────────────
        self._check_billing_abandonment(frame_ts)

    def _update_zone_state(
        self,
        state: TrackState,
        current_zone: Optional[str],
        frame_ts: str,
        conf: float,
        now_ts: float,
    ) -> None:
        sku_zone = (
            self._zones.get_sku_zone(self._camera_id, current_zone)
            if current_zone
            else None
        )
        now_wall = now_ts

        if current_zone != state.current_zone:
            # Zone changed — emit ZONE_EXIT for old zone
            if state.current_zone is not None and state.zone_enter_ts is not None:
                dwell_ms = int((now_wall - state.zone_enter_ts) * 1000)
                old_sku = self._zones.get_sku_zone(self._camera_id, state.current_zone)
                seq = state.next_seq()
                self._emitter.emit_zone_exit(
                    self._store_id, self._camera_id, state.visitor_id,
                    frame_ts, state.current_zone, dwell_ms,
                    conf, state.is_staff, seq, old_sku,
                )

            # Emit ZONE_ENTER for new zone
            if current_zone is not None:
                seq = state.next_seq()
                self._emitter.emit_zone_enter(
                    self._store_id, self._camera_id, state.visitor_id,
                    frame_ts, current_zone, conf, state.is_staff, seq, sku_zone,
                )
                state.zone_enter_ts = now_wall
                state.last_dwell_emit_ts = now_wall
            else:
                state.zone_enter_ts = None
                state.last_dwell_emit_ts = None

            state.current_zone = current_zone

        elif current_zone is not None and state.zone_enter_ts is not None:
            # Same zone — check ZONE_DWELL interval
            elapsed_wall = now_wall - (state.last_dwell_emit_ts or state.zone_enter_ts)
            if elapsed_wall >= self._cfg.zone_dwell_interval_s:
                cumulative_ms = int((now_wall - state.zone_enter_ts) * 1000)
                seq = state.next_seq()
                self._emitter.emit_zone_dwell(
                    self._store_id, self._camera_id, state.visitor_id,
                    frame_ts, current_zone, cumulative_ms,
                    conf, state.is_staff, seq, sku_zone,
                )
                state.last_dwell_emit_ts = now_wall

    def _update_billing_state(
        self,
        state: TrackState,
        current_zone: Optional[str],
        frame_ts: str,
        conf: float,
        now_ts: float,
    ) -> None:
        now_wall = now_ts
        in_billing_now = current_zone in _BILLING_ZONES

        if in_billing_now and not state.in_billing and not state.is_staff:
            # Visitor just entered billing zone
            current_queue_depth = len(self._in_billing_now)
            state.in_billing = True
            state.billing_enter_ts = now_wall
            state.billing_enter_video_ts = frame_ts
            self._in_billing_now.add(state.visitor_id)

            seq = state.next_seq()
            self._emitter.emit_billing_queue_join(
                self._store_id, self._camera_id, state.visitor_id,
                frame_ts, current_queue_depth, conf, state.is_staff, seq,
            )

        elif not in_billing_now and state.in_billing and not state.is_staff:
            # Visitor just left the billing zone
            dwell_ms = (
                int((now_wall - state.billing_enter_ts) * 1000)
                if state.billing_enter_ts
                else 0
            )
            state.in_billing = False
            self._in_billing_now.discard(state.visitor_id)

            # Record for POS correlation check (later in same batch)
            self._pending_billing_exits[state.visitor_id] = (
                frame_ts,
                state.billing_enter_video_ts,
                dwell_ms,
                conf,
                state.is_staff,
                state.session_seq,
            )
            state.billing_enter_ts = None
            state.billing_enter_video_ts = None

    def _check_billing_abandonment(self, current_frame_ts: str) -> None:
        """Check if any pending billing exits can be resolved as abandonments."""
        to_remove = []
        try:
            current_dt = datetime.strptime(current_frame_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return

        for visitor_id, record in list(self._pending_billing_exits.items()):
            exit_ts, enter_ts, dwell_ms, conf, is_staff, seq = record

            if enter_ts is None:
                to_remove.append(visitor_id)
                continue

            try:
                exit_dt = datetime.strptime(exit_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                to_remove.append(visitor_id)
                continue

            elapsed = (current_dt - exit_dt).total_seconds()

            if elapsed >= self._cfg.billing_abandon_window_s:
                # Window has passed — check POS
                has_purchase = _has_pos_within_window(
                    self._pos_records, self._store_id,
                    enter_ts, self._cfg.billing_abandon_window_s,
                )
                if not has_purchase:
                    self._emitter.emit_billing_queue_abandon(
                        self._store_id, self._camera_id, visitor_id,
                        exit_ts, dwell_ms, conf, is_staff, seq + 1,
                    )
                to_remove.append(visitor_id)

        for vid in to_remove:
            self._pending_billing_exits.pop(vid, None)

    def _handle_disappeared(self, frame_ts: str, now_ts: float) -> None:
        """Emit EXIT for tracks that were active last frame but not this frame."""
        disappeared = [tid for tid in self._active if tid not in self._seen_this_frame]
        now_wall = now_ts

        for track_id in disappeared:
            state = self._active.pop(track_id)
            visitor_id = self._gallery.retire_track(track_id) or state.visitor_id
            self._staff.evict(track_id)

            # Close open zone
            if state.current_zone is not None and state.zone_enter_ts is not None:
                dwell_ms = int((now_wall - state.zone_enter_ts) * 1000)
                sku = self._zones.get_sku_zone(self._camera_id, state.current_zone)
                seq = state.next_seq()
                self._emitter.emit_zone_exit(
                    self._store_id, self._camera_id, visitor_id,
                    frame_ts, state.current_zone, dwell_ms,
                    0.5, state.is_staff, seq, sku,
                )

            # Emit EXIT only from entry camera (outbound crossing)
            # For floor/billing cameras a disappearing track doesn't mean they left the store
            if self._camera_type == "entry" and state.has_entered:
                seq = state.next_seq()
                self._emitter.emit_exit(
                    self._store_id, self._camera_id, visitor_id,
                    frame_ts, 0.5, state.is_staff, seq,
                )

            # Clean up billing state
            self._in_billing_now.discard(state.visitor_id)

    def flush_all(self, frame_ts: str, now_ts: float) -> None:
        """Called at end of clip — close all open states."""
        now_wall = now_ts

        for track_id, state in list(self._active.items()):
            # Close any open zone
            if state.current_zone is not None and state.zone_enter_ts is not None:
                dwell_ms = int((now_wall - state.zone_enter_ts) * 1000)
                sku = self._zones.get_sku_zone(self._camera_id, state.current_zone)
                seq = state.next_seq()
                self._emitter.emit_zone_exit(
                    self._store_id, self._camera_id, state.visitor_id,
                    frame_ts, state.current_zone, dwell_ms, 0.5, state.is_staff, seq, sku,
                )

        self._active.clear()

        # Resolve all pending billing exits as abandonments if no POS found
        for visitor_id, record in list(self._pending_billing_exits.items()):
            exit_ts, enter_ts, dwell_ms, conf, is_staff, seq = record
            if enter_ts:
                has_purchase = _has_pos_within_window(
                    self._pos_records, self._store_id, enter_ts,
                    self._cfg.billing_abandon_window_s,
                )
                if not has_purchase:
                    self._emitter.emit_billing_queue_abandon(
                        self._store_id, self._camera_id, visitor_id,
                        exit_ts, dwell_ms, conf, is_staff, seq + 1,
                    )
        self._pending_billing_exits.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Top-level clip processor
# ─────────────────────────────────────────────────────────────────────────────

def process_clip(
    clip_path: str,
    clip_config: dict,
    config: PipelineConfig,
    zone_manager: ZoneManager,
    reid_gallery: ReIDGallery,
    staff_classifier: StaffClassifier,
    emitter: EventEmitter,
    pos_records: list[dict],
) -> dict:
    """Process a single video clip and emit events.

    Returns summary statistics.
    """
    from ultralytics import YOLO

    camera_id: str = clip_config["camera_id"]
    camera_type: str = clip_config.get("camera_type", "floor")
    start_ts_str: str = clip_config.get("clip_start_time", "2026-03-03T10:00:00Z")

    try:
        clip_start = datetime.strptime(start_ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        clip_start = datetime(2026, 3, 3, 10, 0, 0, tzinfo=timezone.utc)

    logger.info("Processing clip: %s (camera=%s, type=%s)", clip_path, camera_id, camera_type)

    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        logger.error("Cannot open clip: %s", clip_path)
        return {"error": f"Cannot open {clip_path}"}

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    effective_fps = native_fps / config.frame_skip

    logger.info(
        "Clip: %.1f fps native, %d frames, processing every %d frames (%.1f fps effective)",
        native_fps, total_frames, config.frame_skip, effective_fps,
    )

    # Load YOLO model (detection only — tracking is done by our layer)
    yolo = YOLO(config.yolo_model)

    state_manager = VisitorStateManager(
        zone_manager=zone_manager,
        reid_gallery=reid_gallery,
        staff_classifier=staff_classifier,
        emitter=emitter,
        config=config,
        camera_id=camera_id,
        camera_type=camera_type,
        pos_records=pos_records,
    )

    frame_idx = 0
    processed_frames = 0
    start_wall = time.monotonic()

    # ByteTrack is managed by ultralytics per-clip; use persist=True for frame-by-frame
    # We reset the tracker between clips by not using persist across clips.
    tracker_results_buffer = None

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1
        if frame_idx % config.frame_skip != 0:
            continue

        processed_frames += 1

        # Run YOLO + ByteTrack
        try:
            results = yolo.track(
                source=frame,
                persist=True,
                tracker=config.tracker_config,
                conf=config.detection_confidence,
                iou=config.nms_iou,
                classes=[0],          # person class only
                imgsz=config.imgsz,
                verbose=False,
                device=config.device or None,
            )
        except Exception as exc:
            logger.warning("YOLO inference error on frame %d: %s", frame_idx, exc)
            continue

        result = results[0] if results else None
        if result is None or result.boxes is None:
            # No detections this frame — still process for disappeared tracks
            state_manager.process_frame(
                frame, np.array([]), np.zeros((0, 4)), np.array([]),
                frame_idx, native_fps, clip_start,
            )
            continue

        boxes = result.boxes
        if boxes.id is None:
            # Tracker hasn't assigned IDs yet (first few frames)
            state_manager.process_frame(
                frame, np.array([]), np.zeros((0, 4)), np.array([]),
                frame_idx, native_fps, clip_start,
            )
            continue

        track_ids = boxes.id.cpu().numpy().astype(int)
        bboxes = boxes.xyxy.cpu().numpy()        # [N, 4]
        confidences = boxes.conf.cpu().numpy()   # [N]

        state_manager.process_frame(
            frame, track_ids, bboxes, confidences,
            frame_idx, native_fps, clip_start,
        )

        if processed_frames % 100 == 0:
            elapsed = time.monotonic() - start_wall
            progress = frame_idx / max(total_frames, 1) * 100
            logger.info(
                "Progress: %.1f%% | processed=%d | elapsed=%.1fs | gallery_size=%d",
                progress, processed_frames, elapsed, reid_gallery.gallery_size(),
            )

        emitter.flush()

    # End of clip — flush all open states
    last_ts = _ts_from_frame(clip_start, frame_idx, native_fps)
    last_epoch = clip_start.timestamp() + frame_idx / native_fps
    state_manager.flush_all(last_ts, last_epoch)
    emitter.flush()

    cap.release()
    # Reset YOLO tracker between clips
    yolo.predictor = None

    elapsed = time.monotonic() - start_wall
    summary = {
        "clip": clip_path,
        "camera_id": camera_id,
        "camera_type": camera_type,
        "frames_total": frame_idx,
        "frames_processed": processed_frames,
        "elapsed_s": round(elapsed, 2),
    }
    logger.info("Clip done: %s", summary)
    return summary
