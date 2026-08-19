#!/usr/bin/env bash
# Live end-to-end proof of cross-camera identity.
#
# Starts the API with the test topology, runs TWO independent camera processes
# on the same scene (the overlapping-pair case), and checks that the API
# resolved people seen on both feeds to ONE global_ref.
#
# This exercises the deployed shape: separate OS processes, real OSNet
# embeddings, HTTP to a shared registry. The unit tests cover the matching
# logic; this covers the wiring between processes.
#
#   bash scripts/verify_cross_camera.sh [seconds]

set -u
cd "$(dirname "$0")/.."

SECS="${1:-45}"
PY=.venv/bin/python
API=http://127.0.0.1:8000

echo "== cleaning up any previous run =="
pkill -f "uvicorn services.api.app" 2>/dev/null
pkill -f "run_cpu.py --config config/cameras.xcam.yaml" 2>/dev/null
sleep 1

echo "== starting API (in-memory store, xcam topology) =="
FINBLADE_INMEMORY=1 FINBLADE_TOPOLOGY=config/topology.xcam.yaml \
  $PY -m uvicorn services.api.app:app --host 127.0.0.1 --port 8000 \
  > scripts/xcam_api.log 2>&1 &
API_PID=$!

for _ in $(seq 1 40); do
  curl -sf "$API/api/v1/identity/stats" >/dev/null 2>&1 && break
  sleep 0.5
done

if ! curl -sf "$API/api/v1/identity/stats" >/dev/null 2>&1; then
  echo "[BLOCKER] API did not come up; see scripts/xcam_api.log"
  tail -20 scripts/xcam_api.log
  kill $API_PID 2>/dev/null
  exit 1
fi
echo "   API up. topology: $(curl -s "$API/api/v1/identity/stats" | \
  $PY -c 'import json,sys; print(json.load(sys.stdin)["topology_source"])')"

echo "== starting two camera processes on the same scene =="
$PY services/inference/run_cpu.py --config config/cameras.xcam.yaml \
    --camera-id CAM-X-01 --port 8091 --api-url $API --seconds "$SECS" \
    > scripts/xcam_a.log 2>&1 &
$PY services/inference/run_cpu.py --config config/cameras.xcam.yaml \
    --camera-id CAM-X-02 --port 8092 --api-url $API --seconds "$SECS" \
    > scripts/xcam_b.log 2>&1 &

echo "   running for ${SECS}s..."
wait %2 %3 2>/dev/null

sleep 2
echo
echo "== ReID status per camera (from the worker logs) =="
grep -h "ReID" scripts/xcam_a.log scripts/xcam_b.log | head -6

echo
echo "== identity registry =="
curl -s "$API/api/v1/identity/stats" | $PY -m json.tool

echo
echo "== identities seen by MORE THAN ONE camera =="
curl -s "$API/api/v1/identity/list?cross_camera_only=true" | $PY -c '
import json, sys
data = json.load(sys.stdin)["identities"]
print("  %d cross-camera identities" % len(data))
for i in data[:10]:
    print("  %s  cameras=%s  samples=%s"
          % (i["global_ref"], i["cameras_seen"], i["samples"]))
    steps = ["%s:%s" % (j["camera_id"], j["zone_id"]) for j in i["journey"][:4]]
    if steps:
        print("      journey: " + " -> ".join(steps))
'

echo
echo "== privacy check: no embedding vectors in any API response =="
for path in "/api/v1/identity/stats" "/api/v1/identity/list"; do
  if curl -s "$API$path" | grep -qiE '"(embedding|embeddings|vectors)"'; then
    echo "  FAIL: $path leaked a vector"
  else
    echo "  ok: $path"
  fi
done

kill $API_PID 2>/dev/null
echo
echo "logs: scripts/xcam_api.log scripts/xcam_a.log scripts/xcam_b.log"
