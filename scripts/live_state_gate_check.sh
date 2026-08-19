#!/usr/bin/env bash
# End-to-end wiring check for write-on-change, against a real uvicorn process.
#
# The unit tests exercise IngestService directly. This starts the actual app on
# a throwaway database, posts a realistic 10-minute sequence over HTTP, and
# checks what landed in each table. It is the difference between "the gate
# works" and "the gate is wired into the thing that serves requests".
set -u
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null

TMP=$(mktemp -d)
trap 'kill ${PID:-0} 2>/dev/null; rm -rf "${TMP}"' EXIT
DB="${TMP}/gate.db"
PORT=8931
KEY="gate-check-key"

FINBLADE_DB="${DB}" FINBLADE_API_KEY="${KEY}" FINBLADE_AUTOSTART_CAMERAS=0 \
  python -m uvicorn services.api.app:app --host 127.0.0.1 --port "${PORT}" \
  --log-level warning >"${TMP}/api.log" 2>&1 &
PID=$!

for _ in $(seq 40); do
  curl -fsS --max-time 1 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && break
  sleep 0.25
done
if ! curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  echo "API did not start:"; cat "${TMP}/api.log"; exit 1
fi

FINBLADE_PORT="${PORT}" FINBLADE_KEY="${KEY}" FINBLADE_DB="${DB}" \
  python scripts/live_state_gate_check.py
