#!/usr/bin/env bash
# Publish BOTH camera clips as RTSP streams at once:
#
#   media/cam-vid-1.mp4  ->  rtsp://127.0.0.1:8554/cam1
#   media/cam-vid-2.mp4  ->  rtsp://127.0.0.1:8554/cam2
#
# The existing scripts/rtsp_stream.sh publishes one clip and blocks, which is
# fine for a single camera but cannot give you a synchronised pair. This starts
# MediaMTX once and runs both publishers in the background, so the two streams
# begin within a few hundred ms of each other — which matters, because the
# cross-camera transit gate compares wall-clock arrival times across processes.
#
#   bash scripts/rtsp_dual.sh          # start both, print URLs, keep running
#   bash scripts/rtsp_dual.sh stop     # stop publishers + server
#
# ffmpeg is NOT installed here, so the runOnDemand approach in config/mediamtx.yml
# (which runs inside the Docker image) will not work bare-metal. This path uses
# the PyAV publisher in scripts/rtsp_stream.py instead.
set -u
cd "$(dirname "$0")/.."

VID1=media/cam-vid-1.mp4
VID2=media/cam-vid-2.mp4
PY=.venv/bin/python

stop_all() {
  echo "[rtsp] stopping publishers + server"
  pkill -f 'scripts/rtsp_stream.py' 2>/dev/null
  pkill -f 'scripts/bin/mediamtx' 2>/dev/null
  sleep 0.5
  echo "[rtsp] stopped"
}

if [ "${1:-}" = "stop" ]; then
  stop_all
  exit 0
fi

for f in "$VID1" "$VID2"; do
  [ -f "$f" ] || { echo "[BLOCKER] missing clip: $f"; exit 1; }
done
[ -x scripts/bin/mediamtx ] || {
  echo "[BLOCKER] MediaMTX missing — run: bash scripts/rtsp_setup.sh"; exit 1; }

# Clean slate: stale publishers from a previous run would fight for the paths.
pkill -f 'scripts/rtsp_stream.py' 2>/dev/null
sleep 0.3

if ! pgrep -f 'scripts/bin/mediamtx' >/dev/null 2>&1; then
  echo "[rtsp] starting MediaMTX on :8554"
  nohup scripts/bin/mediamtx scripts/mediamtx.yml > scripts/mediamtx.log 2>&1 &
  sleep 2
fi

echo "[rtsp] publishing both clips"
nohup $PY scripts/rtsp_stream.py "$VID1" rtsp://127.0.0.1:8554/cam1 \
  > scripts/rtsp_cam1.log 2>&1 &
P1=$!
nohup $PY scripts/rtsp_stream.py "$VID2" rtsp://127.0.0.1:8554/cam2 \
  > scripts/rtsp_cam2.log 2>&1 &
P2=$!
sleep 3

ok=1
for p in $P1 $P2; do
  kill -0 "$p" 2>/dev/null || { echo "[BLOCKER] publisher $p died on startup"; ok=0; }
done
if [ "$ok" = 0 ]; then
  echo "--- scripts/rtsp_cam1.log ---"; tail -8 scripts/rtsp_cam1.log
  echo "--- scripts/rtsp_cam2.log ---"; tail -8 scripts/rtsp_cam2.log
  exit 1
fi

cat <<EOF

[rtsp] streams live:
   rtsp://127.0.0.1:8554/cam1   <- $VID1
   rtsp://127.0.0.1:8554/cam2   <- $VID2

Point the pipeline at them (in another shell, with the API already running):

  .venv/bin/python services/inference/run_cpu.py --config config/cameras.vid1.yaml \\
      --port 8091 --api-url http://127.0.0.1:8000 &
  .venv/bin/python services/inference/run_cpu.py --config config/cameras.vid2.yaml \\
      --port 8092 --api-url http://127.0.0.1:8000 &

Or run the whole thing end to end:  bash scripts/verify_rtsp_cross_camera.sh

Stop with:  bash scripts/rtsp_dual.sh stop
Logs:       scripts/rtsp_cam1.log scripts/rtsp_cam2.log scripts/mediamtx.log
EOF

echo "[rtsp] publishers running in background (PIDs $P1 $P2)"
