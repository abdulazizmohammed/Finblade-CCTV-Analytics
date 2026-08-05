#!/usr/bin/env bash
# Live proof that the security changes hold AND the operator UI still works.
#   bash scripts/smoke_security.sh
cd "$(dirname "$0")/.." || exit 1

export FINBLADE_API_KEY=full-test-key
export FINBLADE_INTEGRATION_KEY=scoped-test-key
export FINBLADE_DB=$(mktemp -u /tmp/sec_XXXX.db)
PORT=8124
LOG=$(mktemp /tmp/sec_log_XXXX)
SECRET='hunter2'
RTSP="rtsp://admin:${SECRET}@192.168.1.50:554/Streaming/Channels/101"

.venv/bin/python -m uvicorn services.api.app:app --port $PORT </dev/null >"$LOG" 2>&1 &
SRV=$!
cleanup() { kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; rm -f "$FINBLADE_DB"; }
trap cleanup EXIT

for _ in $(seq 1 120); do
  curl -sf -o /dev/null -m 2 -H "Authorization: Bearer full-test-key" \
    "localhost:$PORT/api/v1/summary" && break
  sleep 1
done

F="Authorization: Bearer full-test-key"
S="Authorization: Bearer scoped-test-key"
J="Content-Type: application/json"
code() { curl -s -o /dev/null -m 10 -w "%{http_code}" "$@"; }

# Register a camera whose source carries a real credential.
curl -s -m 10 -X POST -H "$F" -H "$J" \
  -d "{\"camera_id\":\"CAM-SEC\",\"site_id\":\"SITE-01\",\"source\":\"$RTSP\"}" \
  "localhost:$PORT/api/v1/cameras" >/dev/null
curl -s -m 10 -X POST -H "$F" -H "$J" \
  -d '{"camera_id":"CAM-SEC","site_id":"SITE-01","ts":'"$(date +%s)"',"health":{"state":"ONLINE"}}' \
  "localhost:$PORT/api/v1/cameras/health" >/dev/null

echo "== 1. RTSP credential must not appear in any read response =="
for route in /api/v1/cameras /api/v1/summary; do
  for label in full scoped; do
    H=$F; [ "$label" = scoped ] && H=$S
    body=$(curl -s -m 10 -H "$H" "localhost:$PORT$route")
    if echo "$body" | grep -q "$SECRET"; then
      echo "  FAIL $route ($label key) LEAKS the password"
    else
      echo "  ok   $route ($label key) — no credential"
    fi
  done
done
echo "  operator sees the field (needed by the Start-pipeline button):"
curl -s -m 10 -H "$F" "localhost:$PORT/api/v1/cameras" \
  | .venv/bin/python -c '
import json,sys
row=[c for c in json.load(sys.stdin)["cameras"] if c["camera_id"]=="CAM-SEC"][0]
print("    source =", repr(row.get("source")), "| truthy:", bool(row.get("source")))'
echo "  integration key sees no source field at all:"
curl -s -m 10 -H "$S" "localhost:$PORT/api/v1/summary" \
  | .venv/bin/python -c '
import json,sys
row=[c for c in json.load(sys.stdin)["cameras"] if c["camera_id"]=="CAM-SEC"][0]
print("    source present:", "source" in row)'

echo
echo "== 2. Saved frames are gated, and still loadable by the UI via ?key= =="
mkdir -p evidence/bookmarks
printf '\xff\xd8\xff\xe0SMOKE' > evidence/bookmarks/smoke_probe.jpg
echo "  /bookmarks/smoke_probe.jpg  no key          -> $(code localhost:$PORT/bookmarks/smoke_probe.jpg) (want 401)"
echo "  /bookmarks/smoke_probe.jpg  ?key=           -> $(code "localhost:$PORT/bookmarks/smoke_probe.jpg?key=full-test-key") (want 200)"
echo "  /media/CAM-SEC_frame.jpg    no key          -> $(code localhost:$PORT/media/CAM-SEC_frame.jpg) (want 401)"
rm -f evidence/bookmarks/smoke_probe.jpg

echo
echo "== 3. Operator UI still bootstraps with NO key =="
for page in /web/dashboard.html /web/history.html /web/cameras.html /web/report.html \
            /web/apikey.js /web/finblade-theme.css /tools/zone-editor.html /openapi.json; do
  printf '  %-32s -> %s (want 200)\n' "$page" "$(code localhost:$PORT$page)"
done

echo
echo "== 4. OpenAPI now documents authentication =="
curl -s -m 10 "localhost:$PORT/openapi.json" | .venv/bin/python -c '
import json,sys
d=json.load(sys.stdin)
print("  securitySchemes:", sorted((d.get("components") or {}).get("securitySchemes",{})))
print("  global security:", d.get("security"))
print("  paths:", len(d["paths"]))'

echo
echo "== 5. Incident frame by alert id (integration route) =="
printf '\xff\xd8\xff\xe0INCIDENT' > evidence/bookmarks/inc_probe.jpg
AID=$(curl -s -m 10 -X POST -H "$F" -H "$J" \
  -d '{"rule_id":"R-06","severity":"RED","message":"intrusion","zone_id":"ZONE-02","camera_id":"CAM-SEC","ts":100.0,"kind":"FIRE","frame":"/bookmarks/inc_probe.jpg"}' \
  "localhost:$PORT/api/v1/alerts" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["alert_id"])')
echo "  alert_id: $AID"
echo "  scoped GET /api/v1/incidents/$AID/frame -> $(code -H "$S" "localhost:$PORT/api/v1/incidents/$AID/frame") (want 200)"
echo "  no key                                  -> $(code "localhost:$PORT/api/v1/incidents/$AID/frame") (want 401)"
echo "  content-type: $(curl -s -o /dev/null -m 10 -w '%{content_type}' -H "$S" "localhost:$PORT/api/v1/incidents/$AID/frame")"
rm -f evidence/bookmarks/inc_probe.jpg

echo
echo "== 6. Health endpoints =="
echo "  /healthz            no key -> $(code localhost:$PORT/healthz) (want 200, open for a load balancer)"
echo "  /readyz             no key -> $(code localhost:$PORT/readyz) (want 200)"
echo "  /api/v1/health      no key -> $(code localhost:$PORT/api/v1/health) (want 401)"
echo "  /api/v1/health      scoped -> $(code -H "$S" localhost:$PORT/api/v1/health) (want 200)"
curl -s -m 10 -H "$S" "localhost:$PORT/api/v1/health" | .venv/bin/python -c '
import json,sys
d=json.load(sys.stdin)
print("  healthy:", d["healthy"], "| components:", sorted(k for k in d["checks"] if k!="ts"))'

echo
echo "== 7. Site summary block =="
curl -s -m 10 -H "$S" "localhost:$PORT/api/v1/summary" | .venv/bin/python -c '
import json,sys
d=json.load(sys.stdin)
print("  site_id:", d["site_id"])
print("  summary:", json.dumps(d["summary"], sort_keys=True))'

echo
echo "== 8. server tracebacks =="
grep -c -i traceback "$LOG" | sed 's/^/  traceback lines: /'
