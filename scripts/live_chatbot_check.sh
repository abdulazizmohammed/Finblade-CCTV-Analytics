#!/usr/bin/env bash
# End-to-end check of the Part B chatbot endpoints against a real uvicorn.
#
# Throwaway database, no cameras, no network beyond localhost. Proves the
# routes are wired and that the tool layer in integrations/finblade_ai produces
# requests they accept — which unit tests against IngestService cannot.
set -u
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null

TMP=$(mktemp -d)
trap 'kill ${PID:-0} 2>/dev/null; rm -rf "${TMP}"' EXIT
DB="${TMP}/chatbot.db"
PORT=8932
KEY="chatbot-check-key"

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

FINBLADE_PORT="${PORT}" FINBLADE_KEY="${KEY}" python scripts/live_chatbot_check.py
rc=$?
[ "${rc}" -ne 0 ] && { echo "--- api.log ---"; tail -30 "${TMP}/api.log"; }
exit "${rc}"
