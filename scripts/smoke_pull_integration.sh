#!/usr/bin/env bash
# Live check of the pull integration (option 1): real app, real store, real HTTP.
#   bash scripts/smoke_pull_integration.sh
# Asserts GET /api/v1/summary works on the scoped key, that the scoped key is
# refused on the destructive routes, and that 401 and 403 stay distinct.
cd "$(dirname "$0")/.." || exit 1

export FINBLADE_API_KEY=full-test-key
export FINBLADE_INTEGRATION_KEY=scoped-test-key
export FINBLADE_DB=$(mktemp -u /tmp/smoke_XXXX.db)
PORT=8123
LOG=$(mktemp /tmp/smoke_log_XXXX)

.venv/bin/python -m uvicorn services.api.app:app --port $PORT </dev/null >"$LOG" 2>&1 &
SRV=$!
cleanup() { kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; rm -f "$FINBLADE_DB"; }
trap cleanup EXIT

for _ in $(seq 1 120); do
  curl -sf -o /dev/null -m 2 -H "Authorization: Bearer full-test-key" \
    "localhost:$PORT/api/v1/summary" && break
  sleep 1
done

echo "== GET /api/v1/summary on the scoped key =="
curl -s -m 10 -H "Authorization: Bearer scoped-test-key" \
  "localhost:$PORT/api/v1/summary" | .venv/bin/python -c '
import sys, json
d = json.load(sys.stdin)
print("sections :", sorted(d.keys()))
print("cameras  :", len(d["cameras"]), "| zones:", len(d["zones"]),
      "| alerts:", len(d["alerts"]))
print("counts   :", d["counts"])
leak = [c["camera_id"] for c in d["cameras"] if "stream_url" in c]
print("stream_url leaked:", leak or "none")
for c in d["cameras"][:3]:
    print("   ", c.get("camera_id"), c.get("effective_state"),
          c.get("snapshot_path"))
'

echo
echo "== status codes =="
code() { curl -s -o /dev/null -m 10 -w "%{http_code}" "$@"; }
S="Authorization: Bearer scoped-test-key"
F="Authorization: Bearer full-test-key"
J="Content-Type: application/json"
printf 'scoped  GET    /summary               -> %s (want 200)\n' "$(code -H "$S" localhost:$PORT/api/v1/summary)"
printf 'scoped  DELETE /alerts                -> %s (want 403)\n' "$(code -X DELETE -H "$S" localhost:$PORT/api/v1/alerts)"
printf 'scoped  DELETE /frames/orphaned       -> %s (want 403)\n' "$(code -X DELETE -H "$S" localhost:$PORT/api/v1/frames/orphaned)"
printf 'scoped  POST   /zones                 -> %s (want 403)\n' "$(code -X POST -H "$S" -H "$J" -d '{}' localhost:$PORT/api/v1/zones)"
printf 'scoped  POST   /identity/tuning       -> %s (want 403)\n' "$(code -X POST -H "$S" -H "$J" -d '{}' localhost:$PORT/api/v1/identity/tuning)"
printf 'scoped  POST   /alerts/1/ack          -> %s (want 409, in scope, no such alert)\n' "$(code -X POST -H "$S" -H "$J" -d '{"acknowledged_by":"op@finblade"}' localhost:$PORT/api/v1/alerts/1/ack)"
printf 'full    DELETE /alerts?scope=closed   -> %s (want 200)\n' "$(code -X DELETE -H "$F" "localhost:$PORT/api/v1/alerts?scope=closed")"
printf 'none    GET    /summary               -> %s (want 401)\n' "$(code localhost:$PORT/api/v1/summary)"
printf 'wrong   GET    /summary               -> %s (want 401)\n' "$(code -H 'Authorization: Bearer nope' localhost:$PORT/api/v1/summary)"
printf 'none    GET    /web/dashboard.html    -> %s (want 200, bootstrap page)\n' "$(code localhost:$PORT/web/dashboard.html)"

echo
echo "== server tracebacks =="
grep -c -i traceback "$LOG" | sed 's/^/traceback lines: /'
