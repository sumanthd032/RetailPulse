#!/bin/sh
# Startup for a single-container host (Render). Render gives us one web
# container with an ephemeral filesystem — no separate ingest container like
# docker-compose has — so we start the API, wait for it to be healthy, then
# seed the SQLite DB from the committed real events. Every cold start rebuilds
# the DB, which keeps the demo self-healing.
set -e

PORT="${PORT:-8000}"

# Start the API in the background.
uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1 &
API_PID=$!

# Wait for /health before seeding.
echo "Waiting for API on :$PORT ..."
for _ in $(seq 1 60); do
    if curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Seed from the real CCTV events shipped in the image (idempotent: resets first).
python scripts/ingest_real.py --api "http://localhost:$PORT" \
    || echo "Ingest failed — serving an empty store (check logs)."

# Hand control back to the API process.
wait "$API_PID"
