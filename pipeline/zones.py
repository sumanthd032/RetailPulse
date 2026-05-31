"""Zone management — loads store_layout.json and classifies track positions.

Each zone polygon is stored in fractional coordinates [0.0, 1.0] relative to
frame dimensions, making the config resolution-independent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from shapely.geometry import Point, Polygon

logger = logging.getLogger(__name__)


@dataclass
class ZoneDefinition:
    zone_id: str
    sku_zone: Optional[str]
    is_staff_zone: bool
    polygon_frac: list[tuple[float, float]]  # fractional coords


@dataclass
class CameraConfig:
    camera_id: str
    camera_type: str  # entry / floor / billing
    zones: list[ZoneDefinition] = field(default_factory=list)
    entry_line_frac: Optional[dict] = None  # {x1,y1,x2,y2}_frac
    inbound_side: str = "right"  # which side of entry line is "inside the store"


@dataclass
class EntryLineCrossing:
    """Records a track crossing the entry threshold line."""

    track_id: int
    visitor_id: str
    direction: str  # "inbound" or "outbound"
    timestamp: str
    confidence: float


class ZoneManager:
    """Manages zone polygons and entry-line logic for a single store."""

    def __init__(self, layout_path: str, store_id: str) -> None:
        self.store_id = store_id
        self._cameras: dict[str, CameraConfig] = {}
        self._shapely_polygons: dict[str, dict[str, Polygon]] = {}  # camera_id -> {zone_id -> Polygon}
        self._load(layout_path)

    def _load(self, layout_path: str) -> None:
        path = Path(layout_path)
        if not path.exists():
            logger.warning("store_layout.json not found at %s — using empty config", layout_path)
            return

        with open(path) as f:
            data = json.load(f)

        for cam_id, cam_data in data.get("cameras", {}).items():
            zones = []
            for z in cam_data.get("zones", []):
                zones.append(ZoneDefinition(
                    zone_id=z["zone_id"],
                    sku_zone=z.get("sku_zone"),
                    is_staff_zone=z.get("is_staff_zone", False),
                    polygon_frac=[(p[0], p[1]) for p in z["polygon_frac"]],
                ))

            entry_line = cam_data.get("entry_line")
            self._cameras[cam_id] = CameraConfig(
                camera_id=cam_id,
                camera_type=cam_data.get("type", "floor"),
                zones=zones,
                entry_line_frac=entry_line,
                inbound_side=cam_data.get("inbound_side", "right"),
            )

        logger.info("Loaded %d camera configs for store %s", len(self._cameras), self.store_id)

    def _get_polygons(self, camera_id: str, frame_w: int, frame_h: int) -> dict[str, Polygon]:
        """Build shapely Polygons in pixel coords for a given frame size."""
        cam = self._cameras.get(camera_id)
        if cam is None:
            return {}

        key = f"{camera_id}_{frame_w}_{frame_h}"
        if key not in self._shapely_polygons:
            polys: dict[str, Polygon] = {}
            for z in cam.zones:
                px_pts = [(x * frame_w, y * frame_h) for x, y in z.polygon_frac]
                polys[z.zone_id] = Polygon(px_pts)
            self._shapely_polygons[key] = polys

        return self._shapely_polygons[key]

    def get_zone(
        self,
        bbox: np.ndarray,
        frame_shape: tuple[int, ...],
        camera_id: str,
    ) -> Optional[str]:
        """Return the zone_id for the bottom-center of a bounding box, or None."""
        h, w = frame_shape[:2]
        x1, y1, x2, y2 = bbox[:4]
        # Bottom-center is more accurate for floor position than centroid
        foot_x = (x1 + x2) / 2.0
        foot_y = float(y2)
        pt = Point(foot_x, foot_y)

        polys = self._get_polygons(camera_id, w, h)
        for zone_id, poly in polys.items():
            if poly.contains(pt):
                return zone_id
        return None

    def get_zone_def(self, camera_id: str, zone_id: str) -> Optional[ZoneDefinition]:
        cam = self._cameras.get(camera_id)
        if cam is None:
            return None
        for z in cam.zones:
            if z.zone_id == zone_id:
                return z
        return None

    def is_staff_zone(self, camera_id: str, zone_id: str) -> bool:
        zd = self.get_zone_def(camera_id, zone_id)
        return zd.is_staff_zone if zd else False

    def get_sku_zone(self, camera_id: str, zone_id: str) -> Optional[str]:
        zd = self.get_zone_def(camera_id, zone_id)
        return zd.sku_zone if zd else None

    def get_camera_type(self, camera_id: str) -> str:
        cam = self._cameras.get(camera_id)
        return cam.camera_type if cam else "unknown"

    def is_billing_camera(self, camera_id: str) -> bool:
        return self.get_camera_type(camera_id) == "billing"

    def get_entry_line(self, camera_id: str, frame_w: int, frame_h: int) -> Optional[tuple]:
        """Returns (x1, y1, x2, y2) in pixel coords or None."""
        cam = self._cameras.get(camera_id)
        if cam is None or cam.entry_line_frac is None:
            return None
        el = cam.entry_line_frac
        return (
            el["x1_frac"] * frame_w,
            el["y1_frac"] * frame_h,
            el["x2_frac"] * frame_w,
            el["y2_frac"] * frame_h,
        )

    def check_line_crossing(
        self,
        prev_foot: tuple[float, float],
        curr_foot: tuple[float, float],
        camera_id: str,
        frame_w: int,
        frame_h: int,
    ) -> Optional[str]:
        """Return 'inbound', 'outbound', or None if no crossing detected."""
        line = self.get_entry_line(camera_id, frame_w, frame_h)
        if line is None:
            return None

        x1, y1, x2, y2 = line
        cam = self._cameras.get(camera_id)
        inbound_side = cam.inbound_side if cam else "right"

        # Determine if the vertical entry line was crossed
        # (works for vertical lines; general case uses cross-product)
        is_vertical = abs(x2 - x1) < abs(y2 - y1)

        if is_vertical:
            prev_side = "right" if prev_foot[0] > x1 else "left"
            curr_side = "right" if curr_foot[0] > x1 else "left"
        else:
            prev_side = "below" if prev_foot[1] > y1 else "above"
            curr_side = "below" if curr_foot[1] > y1 else "above"

        if prev_side == curr_side:
            return None  # no crossing

        if curr_side == inbound_side:
            return "inbound"
        else:
            return "outbound"

    def all_zone_ids(self, camera_id: str) -> list[str]:
        cam = self._cameras.get(camera_id)
        if cam is None:
            return []
        return [z.zone_id for z in cam.zones]

    def known_camera_ids(self) -> list[str]:
        return list(self._cameras.keys())
