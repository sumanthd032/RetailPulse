#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# RetailPulse Detection Pipeline Runner
#
# Usage:
#   ./pipeline/run.sh                              # Process all clips in clips_config.json
#   ./pipeline/run.sh --clip "Resources/CCTV Footage/CAM 1.mp4" --camera-type entry
#   ./pipeline/run.sh --frame-skip 5 --device cpu  # Slower frames, CPU only
#
# Output: data/events.jsonl
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# Activate virtual environment if present
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

echo "============================================================"
echo " RetailPulse Detection Pipeline"
echo " Project root: $PROJECT_ROOT"
echo " Output: data/events.jsonl"
echo "============================================================"

python -m pipeline.run \
    --clips-config data/clips_config.json \
    --layout data/store_layout.json \
    --output data/events.jsonl \
    --pos-csv data/pos_transactions.csv \
    "$@"

echo "Done. Events written to data/events.jsonl"
