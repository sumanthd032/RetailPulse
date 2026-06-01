"""Re-ID gallery and visitor session management.

Maps ByteTrack track IDs (ephemeral) to persistent visitor_ids using
appearance-based gallery matching. Handles three scenarios:

  1. Brief occlusion — ByteTrack keeps the track alive; no Re-ID needed.
  2. Camera handoff — same person appears on a different camera; gallery match
     within a short window assigns the same visitor_id without a new ENTRY.
  3. Re-entry — person exits, returns within 5 minutes; gallery match emits
     REENTRY with the original visitor_id rather than a new ENTRY.

Appearance embedding: combined HSV colour histogram (torso) + body proportion
features. Cosine similarity is used for gallery matching.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

H_BINS, S_BINS, V_BINS = 32, 16, 8  # compact for speed


def _extract_embedding(frame: np.ndarray, bbox: np.ndarray) -> Optional[np.ndarray]:
    """Build a compact appearance descriptor from a detection bbox.

    Combines:
    - HSV colour histogram of the full bbox (dominant visual appearance)
    - HSV colour histogram of the torso region only (most discriminative)
    - Aspect ratio feature (body proportion)
    """
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in bbox[:4])
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    bh = y2 - y1
    bw = x2 - x1
    if bh < 20 or bw < 8:
        return None

    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None

    # Torso region (30–70% height, centre 70% width)
    tx_margin = max(1, int(bw * 0.15))
    ty1_off = int(bh * 0.30)
    ty2_off = int(bh * 0.70)
    torso = crop[ty1_off:ty2_off, tx_margin: bw - tx_margin]
    if torso.size == 0:
        torso = crop

    # Upper body (0–50% height)
    upper = crop[: bh // 2, :]

    def hsv_hist(img: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist(
            [hsv], [0, 1, 2], None, [H_BINS, S_BINS, V_BINS], [0, 180, 0, 256, 0, 256]
        )
        flat = hist.flatten().astype(np.float32)
        norm = np.linalg.norm(flat)
        return flat / norm if norm > 0 else flat

    full_hist = hsv_hist(crop)
    torso_hist = hsv_hist(torso)
    upper_hist = hsv_hist(upper)

    # Body proportion feature (aspect ratio normalised)
    aspect = np.array([min(bh / max(bw, 1), 5.0) / 5.0], dtype=np.float32)

    embedding = np.concatenate([full_hist * 0.3, torso_hist * 0.5, upper_hist * 0.15, aspect * 0.05])
    norm = np.linalg.norm(embedding)
    return embedding / norm if norm > 0 else embedding


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None:
        return 0.0
    dot = float(np.dot(a, b))
    return max(0.0, min(1.0, dot))


@dataclass
class GalleryEntry:
    visitor_id: str
    embedding: np.ndarray
    last_seen_ts: float  # wall-clock seconds (time.monotonic)
    last_camera: str
    is_staff: bool = False


@dataclass
class TrackState:
    """Live state for an active ByteTrack track ID."""

    track_id: int
    visitor_id: str
    store_id: str
    camera_id: str
    is_staff: bool = False

    # Zone state machine
    current_zone: Optional[str] = None
    zone_enter_ts: Optional[float] = None       # wall-clock time entered zone
    last_dwell_emit_ts: Optional[float] = None  # when last ZONE_DWELL was emitted

    # Entry/exit line crossing
    prev_foot: Optional[tuple[float, float]] = None  # (x, y) last frame
    has_entered: bool = False  # True after first ENTRY event emitted

    # Billing
    in_billing: bool = False
    billing_enter_ts: Optional[float] = None
    billing_enter_video_ts: Optional[str] = None  # ISO timestamp for POS correlation

    # Session sequence counter
    session_seq: int = 0

    # Appearance (averaged over stable frames)
    embedding_frames: int = 0
    embedding: Optional[np.ndarray] = None

    # Staff classification confidence
    staff_confidence: float = 0.0

    def update_embedding(self, new_emb: Optional[np.ndarray]) -> None:
        if new_emb is None:
            return
        if self.embedding is None:
            self.embedding = new_emb
        else:
            alpha = 1.0 / (self.embedding_frames + 1)
            self.embedding = (1 - alpha) * self.embedding + alpha * new_emb
            norm = np.linalg.norm(self.embedding)
            if norm > 0:
                self.embedding /= norm
        self.embedding_frames += 1

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq


class ReIDGallery:
    """Manages the appearance gallery and visitor_id assignment."""

    def __init__(
        self,
        similarity_threshold: float = 0.72,
        gallery_ttl_s: int = 300,
        min_stable_frames: int = 5,
    ) -> None:
        self._threshold = similarity_threshold
        self._ttl = gallery_ttl_s
        self._min_stable = min_stable_frames
        self._gallery: dict[str, GalleryEntry] = {}  # visitor_id → entry
        self._track_to_visitor: dict[int, str] = {}  # track_id → visitor_id

    def _new_visitor_id(self) -> str:
        short = uuid.uuid4().hex[:6]
        return f"VIS_{short}"

    def _prune(self, now: float) -> None:
        # `now` is footage time (epoch seconds derived from the frame), not
        # processing wall-clock — so a gallery entry expires after `ttl` seconds
        # of *store* time, which is what cross-camera handoff and re-entry need.
        stale = [vid for vid, e in self._gallery.items() if now - e.last_seen_ts > self._ttl]
        for vid in stale:
            del self._gallery[vid]

    def _best_match(
        self,
        embedding: np.ndarray,
        camera_id: str,
        now: float,
        window_s: int,
    ) -> Optional[str]:
        """Find the best gallery match within recency window."""
        best_sim = self._threshold
        best_vid: Optional[str] = None

        for vid, entry in self._gallery.items():
            age = now - entry.last_seen_ts
            if age > window_s:
                continue
            sim = _cosine_sim(embedding, entry.embedding)
            if sim > best_sim:
                best_sim = sim
                best_vid = vid

        return best_vid

    def register_or_reidentify(
        self,
        track_id: int,
        frame: np.ndarray,
        bbox: np.ndarray,
        camera_id: str,
        camera_type: str,
        now_ts: Optional[float] = None,
        reentry_window_s: int = 300,
    ) -> tuple[str, bool, bool]:
        """Assign or retrieve a visitor_id for a ByteTrack track.

        `now_ts` is footage time (epoch seconds for this frame), so recency
        windows are measured in store time and a person can be matched across
        cameras that are running simultaneously. Falls back to wall-clock when
        not supplied (keeps the gallery usable in isolation, e.g. unit tests).

        Returns (visitor_id, is_reentry, is_brand_new).
        is_reentry=True means this should emit a REENTRY event.
        is_brand_new=True means this should emit an ENTRY event.
        """
        # Already known track → just return existing assignment
        if track_id in self._track_to_visitor:
            return self._track_to_visitor[track_id], False, False

        emb = _extract_embedding(frame, bbox)
        now = now_ts if now_ts is not None else time.monotonic()
        self._prune(now)

        is_reentry = False
        is_brand_new = True

        if emb is not None:
            # Re-entry: full window (5 min)
            match_vid = self._best_match(emb, camera_id, now, reentry_window_s)

            if match_vid and camera_type == "entry":
                # Found a gallery match at the entry camera → this is a re-entry
                visitor_id = match_vid
                is_reentry = True
                is_brand_new = False
                logger.debug("Track %d → REENTRY as %s", track_id, visitor_id)

            elif match_vid and camera_type != "entry":
                # Camera handoff (floor or billing) — same person, different camera
                visitor_id = match_vid
                is_brand_new = False
                logger.debug("Track %d → camera handoff as %s", track_id, visitor_id)

            else:
                visitor_id = self._new_visitor_id()
                logger.debug("Track %d → new visitor %s", track_id, visitor_id)
        else:
            visitor_id = self._new_visitor_id()

        self._track_to_visitor[track_id] = visitor_id

        # Update gallery
        if emb is not None:
            self._gallery[visitor_id] = GalleryEntry(
                visitor_id=visitor_id,
                embedding=emb,
                last_seen_ts=now,
                last_camera=camera_id,
            )

        return visitor_id, is_reentry, is_brand_new

    def update_gallery(
        self,
        track_id: int,
        frame: np.ndarray,
        bbox: np.ndarray,
        camera_id: str,
        now_ts: Optional[float] = None,
        is_staff: bool = False,
    ) -> None:
        """Refresh the gallery embedding with the latest frame data."""
        vid = self._track_to_visitor.get(track_id)
        if vid is None:
            return

        emb = _extract_embedding(frame, bbox)
        if emb is None:
            return

        now = now_ts if now_ts is not None else time.monotonic()
        if vid in self._gallery:
            old = self._gallery[vid]
            # Exponential moving average for stable embedding
            alpha = 0.15
            updated = (1 - alpha) * old.embedding + alpha * emb
            norm = np.linalg.norm(updated)
            updated = updated / norm if norm > 0 else updated
            self._gallery[vid] = GalleryEntry(
                visitor_id=vid,
                embedding=updated,
                last_seen_ts=now,
                last_camera=camera_id,
                is_staff=is_staff,
            )
        else:
            self._gallery[vid] = GalleryEntry(
                visitor_id=vid,
                embedding=emb,
                last_seen_ts=now,
                last_camera=camera_id,
                is_staff=is_staff,
            )

    def retire_track(self, track_id: int) -> Optional[str]:
        """Called when a track is lost. Returns visitor_id for EXIT event."""
        return self._track_to_visitor.pop(track_id, None)

    def get_visitor_id(self, track_id: int) -> Optional[str]:
        return self._track_to_visitor.get(track_id)

    def gallery_size(self) -> int:
        return len(self._gallery)
