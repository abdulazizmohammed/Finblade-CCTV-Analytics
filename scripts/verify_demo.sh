#!/usr/bin/env bash
# Is the demo stack actually working? Prints what a tester should see.
cd "$(dirname "$0")/.." || exit 1
PORT="${PORT:-8000}"
K=$(cat .local_key 2>/dev/null)
I=$(cat .local_integration_key 2>/dev/null)
A="Authorization: Bearer $K"
S="Authorization: Bearer $I"
code() { curl -s -o /dev/null -m 10 -w "%{http_code}" "$@"; }

echo "== processes =="
pgrep -af 'uvicorn services.api.app' | head -1 || echo "  API NOT RUNNING"
pgrep -af 'run_cpu.py' | head -1 || echo "  camera worker NOT RUNNING (may still be loading YOLO)"

echo
echo "== liveness =="
echo "  /healthz            -> $(code localhost:$PORT/healthz)"
echo "  /readyz             -> $(code localhost:$PORT/readyz)"
echo "  /api/v1/health      -> $(code -H "$S" localhost:$PORT/api/v1/health)"

echo
echo "== waiting for the camera to report frames (up to 90s) =="
for i in $(seq 1 90); do
  seen=$(curl -s -m 5 -H "$A" "localhost:$PORT/api/v1/cameras" \
    | .venv/bin/python -c "
import json,sys
try: cams=json.load(sys.stdin)['cameras']
except Exception: print(''); raise SystemExit
print(next((c.get('state') or '' for c in cams if c['camera_id']=='CAM-01'), ''))
" 2>/dev/null)
  [ -n "$seen" ] && { echo "  camera state after ${i}s: $seen"; break; }
  sleep 1
done

echo
echo "== live summary (integration key) =="
curl -s -m 10 -H "$S" "localhost:$PORT/api/v1/summary" | .venv/bin/python -c "
import json,sys
d=json.load(sys.stdin)
print('  site_id :', d['site_id'])
print('  summary :', json.dumps(d['summary'], sort_keys=True))
for c in d['cameras']:
    print('  camera  : %s  state=%s  fps=%s  res=%s  people_in_view=%s' % (
        c['camera_id'], c.get('effective_state'), c.get('input_fps'),
        c.get('resolution'), c.get('people_in_view')))
    print('            snapshot_path=%s' % c.get('snapshot_path'))
    print('            source field present: %s' % ('source' in c))
print('  zones   :', len(d['zones']), '(0 means no polygons drawn yet — expected)')
print('  alerts  :', len(d['alerts']))
"

echo
echo "== annotated snapshot =="
BYTES=$(curl -s -m 15 -H "$S" "localhost:$PORT/api/v1/cameras/CAM-01/snapshot" -o /tmp/demo_snap.jpg -w '%{size_download}')
echo "  /api/v1/cameras/CAM-01/snapshot -> ${BYTES} bytes, saved /tmp/demo_snap.jpg"
cp /tmp/demo_snap.jpg evidence/demo_snapshot.jpg 2>/dev/null && \
  echo "  copied to evidence/demo_snapshot.jpg — OPEN THIS: are the boxes on people?"

echo
echo "== security spot checks with the integration key =="
echo "  DELETE /api/v1/alerts        -> $(code -X DELETE -H "$S" localhost:$PORT/api/v1/alerts)  (want 403)"
echo "  POST   /api/v1/zones         -> $(code -X POST -H "$S" -H 'Content-Type: application/json' -d '{}' localhost:$PORT/api/v1/zones)  (want 403)"
echo "  GET    /bookmarks/ no key    -> $(code localhost:$PORT/bookmarks/anything.jpg)  (want 401)"
echo "  credential in /api/v1/cameras: $(curl -s -m 10 -H "$A" localhost:$PORT/api/v1/cameras | grep -c 'rtsp://[^*]*:[^*]*@') (want 0)"
