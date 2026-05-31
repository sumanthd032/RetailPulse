"""Staff classification — two-signal approach.

Signal 1: HSV color histogram of the torso region.
  Staff wear a solid-color uniform. We find the staff reference color from the
  first minute of footage (staff are present near the register from store-open),
  then check subsequent detections against this reference.

Signal 2: Trajectory pattern.
  Staff spend most of their time in staff-designated zones (behind the counter,
  at the register). A track that spends >60% of its detected time in those zones
  over a 5-minute window is classified as staff.

Final: is_staff = color_signal OR (trajectory_signal AND duration >= 5 min)
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# HSV bins for color histogram
H_BINS = 32
S_BINS = 32
V_BINS = 16
HIST_BINS = (H_BINS, S_BINS, V_BINS)
HIST_RANGES = [0, 180, 0, 256, 0, 256]


def _extract_torso(frame: np.ndarray, bbox: np.ndarray) -> Optional[np.ndarray]:
    """Extract the torso region — top 40–70% of bbox, center 60% width."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in bbox[:4])

    # Clamp to frame
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    bh = y2 - y1
    bw = x2 - x1
    if bh < 20 or bw < 10:
        return None

    # Torso: vertically 30–70%, horizontally centre 60%
    ty1 = y1 + int(bh * 0.30)
    ty2 = y1 + int(bh * 0.70)
    tx_margin = int(bw * 0.20)
    tx1 = x1 + tx_margin
    tx2 = x2 - tx_margin

    region = frame[ty1:ty2, tx1:tx2]
    if region.size == 0:
        return None
    return region


def _compute_hsv_hist(region: np.ndarray) -> np.ndarray:
    """Compute a normalised 3D HSV histogram for a BGR image region."""
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist(
        [hsv], [0, 1, 2], None, HIST_BINS, HIST_RANGES
    )
    hist = hist.flatten().astype(np.float32)
    norm = np.linalg.norm(hist)
    if norm > 0:
        hist /= norm
    return hist


def _dominant_hsv(region: np.ndarray) -> Optional[tuple[float, float, float]]:
    """Return (H, S, V) of the dominant colour via pixel mean on mask."""
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    # Focus on reasonably saturated pixels (not white/black backgrounds)
    mask = cv2.inRange(hsv, (0, 30, 50), (180, 255, 255))
    if cv2.countNonZero(mask) < 50:
        return None
    h_mean = float(cv2.mean(hsv[:, :, 0], mask=mask)[0])
    s_mean = float(cv2.mean(hsv[:, :, 1], mask=mask)[0])
    v_mean = float(cv2.mean(hsv[:, :, 2], mask=mask)[0])
    return h_mean, s_mean, v_mean


@dataclass
class TrackColorHistory:
    """Running HSV histogram for a track (rolling average)."""

    hist: Optional[np.ndarray] = None
    frame_count: int = 0

    def update(self, region: np.ndarray) -> None:
        h = _compute_hsv_hist(region)
        if self.hist is None:
            self.hist = h
        else:
            alpha = 1.0 / (self.frame_count + 1)
            self.hist = (1 - alpha) * self.hist + alpha * h
        self.frame_count += 1


@dataclass
class ZoneTimeRecord:
    """Tracks how much time a track spends in staff vs non-staff zones."""

    staff_zone_frames: int = 0
    total_frames: int = 0

    def update(self, in_staff_zone: bool) -> None:
        self.total_frames += 1
        if in_staff_zone:
            self.staff_zone_frames += 1

    @property
    def staff_fraction(self) -> float:
        if self.total_frames == 0:
            return 0.0
        return self.staff_zone_frames / self.total_frames


class StaffClassifier:
    """Classifies each track as staff or customer."""

    def __init__(
        self,
        color_tolerance_h: int = 20,
        color_tolerance_s: int = 25,
        trajectory_threshold: float = 0.60,
        min_duration_frames: int = 150,  # ~5 min at 5fps = 1500 frames → 150 at 5fps
    ) -> None:
        self._tolerance_h = color_tolerance_h
        self._tolerance_s = color_tolerance_s
        self._traj_threshold = trajectory_threshold
        self._min_duration_frames = min_duration_frames

        # Reference staff colour (H, S range) — calibrated from footage
        self._staff_ref_hsv: Optional[tuple[float, float, float]] = None
        self._calibrated = False

        # Per-track state
        self._color_history: dict[int, TrackColorHistory] = defaultdict(TrackColorHistory)
        self._zone_time: dict[int, ZoneTimeRecord] = defaultdict(ZoneTimeRecord)
        self._confirmed_staff: set[int] = set()
        self._confirmed_customer: set[int] = set()

        # Calibration buffer — first N detections per track near counter
        self._calib_buffer: list[np.ndarray] = []
        self._calib_frames_needed = 30  # collect from ~6 seconds of footage
        self._calib_complete = False

    def calibrate_from_frame(
        self,
        frame: np.ndarray,
        detections: list[tuple[np.ndarray, str]],  # (bbox, zone_id)
    ) -> None:
        """Accumulate colour samples near the cash counter for calibration."""
        if self._calib_complete:
            return

        for bbox, zone_id in detections:
            if zone_id in ("CASH_COUNTER", "BILLING_QUEUE"):
                region = _extract_torso(frame, bbox)
                if region is not None:
                    self._calib_buffer.append(region)

        if len(self._calib_buffer) >= self._calib_frames_needed:
            self._finish_calibration()

    def _finish_calibration(self) -> None:
        all_hsv: list[tuple[float, float, float]] = []
        for region in self._calib_buffer:
            dom = _dominant_hsv(region)
            if dom:
                all_hsv.append(dom)

        if len(all_hsv) >= 5:
            h_vals = [v[0] for v in all_hsv]
            s_vals = [v[1] for v in all_hsv]
            v_vals = [v[2] for v in all_hsv]
            # Median is more robust than mean for colour calibration
            self._staff_ref_hsv = (
                float(np.median(h_vals)),
                float(np.median(s_vals)),
                float(np.median(v_vals)),
            )
            self._calib_complete = True
            logger.info(
                "Staff colour calibrated: H=%.1f S=%.1f V=%.1f",
                *self._staff_ref_hsv,
            )
        else:
            # Not enough data — keep collecting
            self._calib_buffer.clear()

    def _color_signal(self, track_id: int, frame: np.ndarray, bbox: np.ndarray) -> bool:
        """Returns True if the track's torso colour matches the staff reference."""
        if not self._calib_complete or self._staff_ref_hsv is None:
            return False

        region = _extract_torso(frame, bbox)
        if region is None:
            return False

        self._color_history[track_id].update(region)
        dom = _dominant_hsv(region)
        if dom is None:
            return False

        ref_h, ref_s, _ = self._staff_ref_hsv
        h, s, _ = dom

        # Circular hue distance
        h_diff = min(abs(h - ref_h), 180 - abs(h - ref_h))
        return h_diff <= self._tolerance_h and abs(s - ref_s) <= self._tolerance_s

    def update(
        self,
        track_id: int,
        frame: np.ndarray,
        bbox: np.ndarray,
        zone_id: Optional[str],
        is_known_staff_zone: bool,
        fps: float = 5.0,
    ) -> tuple[bool, float]:
        """Update classification state for a track.

        Returns (is_staff, confidence).
        """
        if track_id in self._confirmed_staff:
            return True, 1.0
        if track_id in self._confirmed_customer:
            return False, 0.9

        color_pos = self._color_signal(track_id, frame, bbox)
        self._zone_time[track_id].update(is_known_staff_zone)

        zt = self._zone_time[track_id]
        traj_pos = (
            zt.staff_fraction >= self._traj_threshold
            and zt.total_frames >= self._min_duration_frames
        )

        if color_pos or traj_pos:
            # Only confirm as staff if colour signal fires or long trajectory signal
            if zt.total_frames >= 15 and color_pos:  # must have seen them for ~3s
                self._confirmed_staff.add(track_id)
                logger.debug("Track %d confirmed staff (colour signal)", track_id)
                return True, 0.92
            if traj_pos:
                self._confirmed_staff.add(track_id)
                logger.debug("Track %d confirmed staff (trajectory signal)", track_id)
                return True, 0.88

        # Early frames — undecided
        return False, 0.0

    def evict(self, track_id: int) -> None:
        """Remove state for a track that has exited."""
        self._color_history.pop(track_id, None)
        self._zone_time.pop(track_id, None)
        self._confirmed_staff.discard(track_id)
        self._confirmed_customer.discard(track_id)
