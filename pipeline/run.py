"""CLI entrypoint for the detection pipeline.

Usage:
    python -m pipeline.run [OPTIONS]

Options:
    --clips-config  Path to clips_config.json  (default: data/clips_config.json)
    --layout        Path to store_layout.json  (default: data/store_layout.json)
    --output        Output JSONL path          (default: data/events.jsonl)
    --pos-csv       POS transactions CSV       (default: data/pos_transactions.csv)
    --store-id      Override store ID
    --frame-skip    Frame skip (default 3)
    --device        Torch device (cpu/cuda:0)
    --clip          Process a single clip path (bypasses clips_config.json)
    --camera-id     Camera ID for single-clip mode
    --camera-type   Camera type for single-clip mode (entry/floor/billing)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.config import PipelineConfig
from pipeline.detect import _load_pos_transactions, process_clip
from pipeline.emit import EventEmitter
from pipeline.staff import StaffClassifier
from pipeline.tracker import ReIDGallery
from pipeline.zones import ZoneManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("pipeline.run")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RetailPulse Detection Pipeline")
    p.add_argument("--clips-config", default="data/clips_config.json")
    p.add_argument("--layout", default="data/store_layout.json")
    p.add_argument("--output", default="data/events.jsonl")
    p.add_argument("--pos-csv", default="data/pos_transactions.csv")
    p.add_argument("--store-id", default=None)
    p.add_argument("--frame-skip", type=int, default=3)
    p.add_argument("--device", default="")
    # Single-clip mode
    p.add_argument("--clip", default=None, help="Process a single clip path")
    p.add_argument("--camera-id", default="CAM_ENTRY_01")
    p.add_argument("--camera-type", default="entry", choices=["entry", "floor", "billing"])
    p.add_argument("--clip-start", default="2026-03-03T10:00:00Z")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    config = PipelineConfig.from_env()
    config.frame_skip = args.frame_skip
    config.output_path = args.output
    config.pos_csv_path = args.pos_csv
    config.store_layout_path = args.layout
    config.device = args.device
    if args.store_id:
        config.store_id = args.store_id

    # Shared components (persist across clips for gallery continuity)
    zone_manager = ZoneManager(config.store_layout_path, config.store_id)
    reid_gallery = ReIDGallery(
        similarity_threshold=config.reid_similarity_threshold,
        gallery_ttl_s=config.reid_gallery_ttl_seconds,
        min_stable_frames=config.reid_min_stable_frames,
    )
    staff_classifier = StaffClassifier(
        color_tolerance_h=config.staff_color_tolerance,
        trajectory_threshold=config.staff_trajectory_threshold,
    )
    pos_records = _load_pos_transactions(config.pos_csv_path)
    emitter = EventEmitter(config.output_path)

    logger.info("Starting RetailPulse pipeline")
    logger.info("Output: %s", config.output_path)
    logger.info("POS records loaded: %d", len(pos_records))

    summaries = []

    if args.clip:
        # Single-clip mode
        clip_config = {
            "clip_path": args.clip,
            "store_id": config.store_id,
            "camera_id": args.camera_id,
            "camera_type": args.camera_type,
            "clip_start_time": args.clip_start,
        }
        summary = process_clip(
            clip_path=args.clip,
            clip_config=clip_config,
            config=config,
            zone_manager=zone_manager,
            reid_gallery=reid_gallery,
            staff_classifier=staff_classifier,
            emitter=emitter,
            pos_records=pos_records,
        )
        summaries.append(summary)
    else:
        # Multi-clip mode from clips_config.json
        clips_path = Path(args.clips_config)
        if not clips_path.exists():
            logger.error("clips_config.json not found at %s", clips_path)
            sys.exit(1)

        with open(clips_path) as f:
            clips = json.load(f)

        logger.info("Processing %d clips from %s", len(clips), clips_path)

        for clip_cfg in clips:
            clip_path = clip_cfg["clip_path"]

            # Skip non-customer cameras (stockroom, etc.)
            if clip_cfg.get("camera_type") == "stockroom":
                logger.info("Skipping stockroom camera: %s", clip_path)
                continue

            if not Path(clip_path).exists():
                logger.warning("Clip not found, skipping: %s", clip_path)
                continue

            # Per-clip store_id override
            if args.store_id:
                clip_cfg["store_id"] = args.store_id
            config.store_id = clip_cfg.get("store_id", config.store_id)

            summary = process_clip(
                clip_path=clip_path,
                clip_config=clip_cfg,
                config=config,
                zone_manager=zone_manager,
                reid_gallery=reid_gallery,
                staff_classifier=staff_classifier,
                emitter=emitter,
                pos_records=pos_records,
            )
            summaries.append(summary)

    written, rejected = emitter.close()

    logger.info("=" * 60)
    logger.info("Pipeline complete")
    logger.info("Events written:   %d", written)
    logger.info("Events rejected:  %d", rejected)
    logger.info("Clips processed:  %d", len(summaries))
    logger.info("Output:           %s", config.output_path)

    for s in summaries:
        logger.info("  %s", s)


if __name__ == "__main__":
    main()
