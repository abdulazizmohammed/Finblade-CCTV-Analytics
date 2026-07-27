#!/usr/bin/env bash
# Bring the whole stack UP and LEAVE IT RUNNING.
#
# Unlike scripts/demo.sh and the verify_* scripts — which run in the foreground
# and tear everything down on exit — this detaches every process so the system
# stays up after the shell closes. That is what you want when you actually
# intend to use the dashboard rather than prove a code path works.
#
#   bash scripts/start_stack.sh            # RTSP cameras (cam-vid-1/2)
#   bash scripts/start_stack.sh files      # file-source cameras (no RTSP)
#   bash scripts/stop_all.sh               # stop everything
#
# Persists to data/finblade.db (NOT in-memory), so history and reports survive.
set -u
cd "$(dirname "$0")/.."

MODE="${1:-rtsp}"
PY=.venv/bin/python
API=http://127.0.0.1:8000

echo "== stopping anything already running =="
bash scripts/stop_all.sh >/dev/null 2>&1
sleep 1

if [ "$MODE" = "rtsp" ]; then
  echo "== publishing clips over RTSP =="
  bash scripts/rtsp_dual.sh >/dev/null 2>&1 || {
    echo "[BLOCKER] RTSP publish failed — run: bash scripts/rtsp_dual.sh"; exit 1; }
  CFG1=config/cameras.vid1.yaml
  CFG2=config/cameras.vid2.yaml
  TOPO=config/topology.vid.yaml
else
  CFG1=config/cameras.synthetic.yaml
  CFG2=config/cameras.cam2.yaml
  TOPO=config/topology.yaml
fi

echo "== starting API on :8000 (SQLite, topology=$TOPO) =="
FINBLADE_TOPOLOGY="$TOPO" nohup $PY -m uvicorn services.api.app:app \
  --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 1

up=0
for _ in $(seq 1 40); do
  if curl -sf "$API/api/v1/identity/stats" >/dev/null 2>&1; then up=1; break; fi
  sleep 0.5
done
if [ "$up" = 0 ]; then
  echo "[BLOCKER] API did not come up; see scripts/api.log"; tail -20 scripts/api.log; exit 1
fi

echo "== starting cameras =="
nohup $PY services/inference/run_cpu.py --config "$CFG1" \
  --port 8091 --stream-host localhost --api-url $API > scripts/cam1.log 2>&1 &
nohup $PY services/inference/run_cpu.py --config "$CFG2" \
  --port 8092 --stream-host localhost --api-url $API > scripts/cam2.log 2>&1 &
sleep 8   # let them open the source, load the models and post a first heartbeat

echo
echo "== status =="
ps -eo pid,etime,cmd | grep -E 'uvicorn services.api|run_cpu.py|mediamtx|rtsp_stream.py' \
  | grep -v grep | sed 's/^/  /'

echo
echo "== health =="
# Health fields are top-level on each camera, not nested under "health".
# Cameras from previous sessions persist in data/finblade.db, so flag anything
# that has not reported recently rather than listing it as if it were live.
curl -s "$API/api/v1/cameras" | $PY -c '
import json, sys, time
try:
    cams = json.load(sys.stdin).get("cameras", [])
except Exception:
    cams = []
if not cams:
    print("  no cameras registered yet (give them a few more seconds)")
now = time.time()
stale = 0
for c in sorted(cams, key=lambda x: x.get("last_seen") or 0, reverse=True):
    age = now - (c.get("last_seen") or 0)
    if age > 120:
        stale += 1
        continue
    print("  %-12s %-12s fps=%s" % (c.get("camera_id"),
          c.get("effective_state", "?"), c.get("input_fps", "?")))
if stale:
    print("  (+%d camera(s) from earlier sessions, not reporting - see note below)" % stale)
' 2>/dev/null || echo "  (cameras endpoint not ready)"

curl -s "$API/api/v1/identity/stats" | $PY -c '
import json, sys
d = json.load(sys.stdin)
print("  identity: %d identities, site_occupancy=%d, cross_camera=%d, topology=%s"
      % (d["identities"], d["site_occupancy"], d["cross_camera"], d["topology_source"]))
' 2>/dev/null

cat <<EOF

== open ==
  Dashboard   http://localhost:8000/web/dashboard.html
  Cameras     http://localhost:8000/web/cameras.html
  History     http://localhost:8000/web/history.html
  Report      http://localhost:8000/web/report.html
  Zone editor http://localhost:8000/tools/zone-editor.html
  Feeds       http://localhost:8091  and  http://localhost:8092

Stop everything:  bash scripts/stop_all.sh
Logs: scripts/api.log scripts/cam1.log scripts/cam2.log
EOF
