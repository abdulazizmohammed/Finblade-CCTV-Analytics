#!/usr/bin/env bash
# End-to-end over the REAL RTSP path: publish both clips, run both cameras
# against rtsp:// sources, and check cross-camera identity resolved.
#
# Differs from scripts/verify_cross_camera.sh, which reads files directly. This
# one exercises the network path a real deployment uses — MediaMTX, RTSP
# transport, reconnect handling — so it can catch decode/timing problems that a
# file source hides.
#
#   bash scripts/verify_rtsp_cross_camera.sh [seconds]
set -u
cd "$(dirname "$0")/.."

SECS="${1:-45}"
PY=.venv/bin/python
API=http://127.0.0.1:8000

cleanup() {
  kill "${API_PID:-0}" 2>/dev/null
  pkill -f 'run_cpu.py --config config/cameras.vid' 2>/dev/null
  bash scripts/rtsp_dual.sh stop >/dev/null 2>&1
}
trap cleanup EXIT

echo "== cleaning up any previous run =="
pkill -f "uvicorn services.api.app" 2>/dev/null
pkill -f 'run_cpu.py --config config/cameras.vid' 2>/dev/null
sleep 1

echo "== publishing both clips over RTSP =="
bash scripts/rtsp_dual.sh | sed 's/^/   /' || exit 1

echo "== starting API (in-memory, vid topology) =="
FINBLADE_INMEMORY=1 FINBLADE_TOPOLOGY=config/topology.vid.yaml \
  $PY -m uvicorn services.api.app:app --host 127.0.0.1 --port 8000 \
  > scripts/vid_api.log 2>&1 &
API_PID=$!

for _ in $(seq 1 40); do
  curl -sf "$API/api/v1/identity/stats" >/dev/null 2>&1 && break
  sleep 0.5
done
if ! curl -sf "$API/api/v1/identity/stats" >/dev/null 2>&1; then
  echo "[BLOCKER] API did not come up; see scripts/vid_api.log"
  tail -20 scripts/vid_api.log
  exit 1
fi
echo "   topology: $(curl -s "$API/api/v1/identity/stats" | \
  $PY -c 'import json,sys; print(json.load(sys.stdin)["topology_source"])')"

echo "== starting both cameras on their RTSP streams =="
$PY services/inference/run_cpu.py --config config/cameras.vid1.yaml \
    --port 8091 --api-url $API --seconds "$SECS" > scripts/vid_cam1.log 2>&1 &
$PY services/inference/run_cpu.py --config config/cameras.vid2.yaml \
    --port 8092 --api-url $API --seconds "$SECS" > scripts/vid_cam2.log 2>&1 &

echo "   running for ${SECS}s..."
wait %2 %3 2>/dev/null
sleep 2

echo
echo "== per-camera result =="
$PY - <<'PYEOF'
import json, os
for cam in ("CAM-VID-1", "CAM-VID-2"):
    p = f"evidence/metrics_{cam}.json"
    if not os.path.exists(p):
        print(f"  {cam}: NO METRICS — camera did not run, see scripts/vid_cam*.log")
        continue
    m = json.load(open(p))
    d = m["detections_per_frame"]
    print(f"  {cam}: fps={m['avg_fps']} frames={m['frames_processed']} "
          f"dets/frame avg={d['avg']} max={d['max']} tracks={m['unique_track_ids']}")
    print(f"      reid={json.dumps(m['reid'])}")
PYEOF

echo
echo "== identity registry =="
curl -s "$API/api/v1/identity/stats" | $PY -m json.tool

echo
echo "== identities seen by BOTH cameras =="
curl -s "$API/api/v1/identity/list?cross_camera_only=true" | $PY -c '
import json, sys
data = json.load(sys.stdin)["identities"]
print("  %d cross-camera identities" % len(data))
for i in data[:12]:
    print("  %s  cameras=%s  samples=%s"
          % (i["global_ref"], i["cameras_seen"], i["samples"]))
'

echo
echo "logs: scripts/vid_api.log scripts/vid_cam1.log scripts/vid_cam2.log"
echo "      scripts/rtsp_cam1.log scripts/rtsp_cam2.log"
