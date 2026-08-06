#!/usr/bin/env bash
# What the FinBlade chart tag actually looks like on a running instance.
cd "$(dirname "$0")/.." || exit 1
export FINBLADE_API_KEY=chart-demo-key
export FINBLADE_SITE_ID=SITE-01
export FINBLADE_DB=$(mktemp -u /tmp/charts_XXXX.db)
PORT=8129
LOG=$(mktemp /tmp/charts_log_XXXX)

.venv/bin/python -m uvicorn services.api.app:app --port $PORT \
  </dev/null >"$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null; rm -f "$FINBLADE_DB"' EXIT
A="Authorization: Bearer $FINBLADE_API_KEY"
J="Content-Type: application/json"

for _ in $(seq 1 120); do
  curl -sf -o /dev/null -m 2 -H "$A" "localhost:$PORT/api/v1/summary" && break
  sleep 1
done

NOW=$(date +%s)
seed_zone() {   # id name occupancy
  RESP=$(curl -s -m 10 -X POST "localhost:$PORT/api/v1/zones/state" -H "$A" -H "$J" \
    -d "{\"zone_id\":\"$1\",\"camera_id\":\"CAM-01\",\"zone_name\":\"$2\",\"occupancy\":$3,\"density\":$4,\"capacity_pct\":$5,\"inflow_per_min\":3.0,\"outflow_per_min\":2.0,\"status\":\"NORMAL\",\"ts\":$NOW}")
  echo "  seed $1: $RESP"
}
echo "== seeding zone state =="
seed_zone ZONE-01 Lobby       4 0.8 40.0
seed_zone ZONE-02 Reception   2 0.4 20.0
seed_zone ZONE-03 1F-Passage  0 0.0 0.0
curl -s -m 10 -X POST "localhost:$PORT/api/v1/cameras/health" -H "$A" -H "$J" \
  -d "{\"camera_id\":\"CAM-01\",\"site_id\":\"SITE-01\",\"ts\":$NOW,
       \"health\":{\"state\":\"ONLINE\",\"input_fps\":24.0}}" >/dev/null
curl -s -m 10 -X POST "localhost:$PORT/api/v1/alerts" -H "$A" -H "$J" \
  -d '{"rule_id":"R-01","severity":"AMBER","message":"density","zone_id":"ZONE-01","camera_id":"CAM-01","ts":'"$NOW"',"kind":"FIRE"}' >/dev/null

# latest_zone_states() is cached for 1s server-side (sqlite_store._ZONE_CACHE_TTL)
# and the readiness loop above already filled that cache while it was empty.
# Reading inside the window returns the empty snapshot and looks like the seeds
# were rejected, which they were not.
sleep 2

echo "=============================================================="
echo "GET /api/v1/zones/state   ->  finblade block"
echo "=============================================================="
curl -s -m 10 -H "$A" "localhost:$PORT/api/v1/zones/state" \
  | .venv/bin/python -c "import json,sys; print(json.dumps(json.load(sys.stdin)['finblade'], indent=2))"

echo
echo "=============================================================="
echo "GET /api/v1/summary       ->  chart ids + titles"
echo "=============================================================="
curl -s -m 10 -H "$A" "localhost:$PORT/api/v1/summary" | .venv/bin/python -c "
import json,sys
d=json.load(sys.stdin)
for c in d['finblade']['charts']:
    extra = c.get('value', c.get('labels'))
    print('  %-22s %-9s %-38s %s' % (c['id'], c['type'], c.get('title',''), extra))
"

echo
echo "=============================================================="
echo "payload cost of the tag"
echo "=============================================================="
for route in /api/v1/zones/state /api/v1/summary; do
  ON=$(curl -s -m 10 -H "$A" "localhost:$PORT$route" | wc -c)
  OFF=$(curl -s -m 10 -H "$A" "localhost:$PORT$route?charts=0" | wc -c)
  printf '  %-24s with=%6s  without=%6s  (+%s bytes)\n' "$route" "$ON" "$OFF" "$((ON-OFF))"
done
