#!/usr/bin/env bash
# Live check for the part unit tests can't cover: that the retention loop is
# wired into the app lifespan and reports itself on /api/v1/health.
#
# Pruning behaviour itself is covered by tests/test_retention.py against both
# stores — this only proves the wiring.
cd "$(dirname "$0")/.." || exit 1
export FINBLADE_API_KEY=ret-test-key
export FINBLADE_RETENTION_DAYS=7
export FINBLADE_RETENTION_INTERVAL=2
export FINBLADE_DB=$(mktemp -u /tmp/ret_XXXX.db)
PORT=8131
LOG=$(mktemp /tmp/ret_log_XXXX)
A="Authorization: Bearer $FINBLADE_API_KEY"

.venv/bin/python -m uvicorn services.api.app:app --port $PORT </dev/null >"$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null; rm -f "$FINBLADE_DB"' EXIT

up=0
for _ in $(seq 1 120); do
  if curl -sf -o /dev/null -m 2 -H "$A" "localhost:$PORT/api/v1/summary"; then
    up=1; break
  fi
  sleep 1
done
if [ "$up" != 1 ]; then
  echo "[BLOCKER] API did not come up:"; tail -20 "$LOG"; exit 1
fi

echo "== waiting for two retention passes (interval 2s) =="
sleep 6

echo
echo "== /api/v1/health -> checks.retention =="
curl -s -m 10 -H "$A" "localhost:$PORT/api/v1/health" \
  | .venv/bin/python -c "
import json, sys
raw = sys.stdin.read()
try:
    r = json.loads(raw)['checks']['retention']
except Exception as exc:
    print('  could not parse health response:', exc); print('  raw:', raw[:200]); raise SystemExit(1)
for k in ('ok', 'enabled', 'days', 'runs', 'errors', 'last_deleted'):
    print('  %-13s %s' % (k, r.get(k)))
"
# NOTE: no `</dev/null` here. Redirecting stdin overrides the pipe, so python
# reads nothing and the JSON parse fails on an empty string — which is what
# made this script look like a broken health endpoint when the endpoint was
# returning 200 the whole time.

echo
echo "== retention log lines =="
grep -i retention "$LOG" | sed 's/^/  /' | head -5 || echo "  (none)"
