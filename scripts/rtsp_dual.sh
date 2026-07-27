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
#   bash scripts/rtsp_dual.sh          # start both, cam2 lagging cam1 by 5s
#   bash scripts/rtsp_dual.sh 0        # start both simultaneously
#   bash scripts/rtsp_dual.sh 12       # cam2 lags cam1 by 12s
#   bash scripts/rtsp_dual.sh stop     # stop publishers + server
#
# THE GAP: cam2's publisher starts N seconds after cam1's, so cam2 shows the
# same scene N seconds behind. Since both publishers loop the same 15s clip, the
# offset holds steady (modulo the clip length). Use it to give the cross-camera
# transit gate a non-zero dt to work with instead of everything arriving at once.
#
# What it does NOT simulate: a true handover. The clips are identical, so a
# person on screen for longer than the gap is visible on BOTH cameras at the
# same wall-clock moment, just at different points in their walk — a lagged
# duplicate, not someone leaving one camera and later entering another. For a
# real handover you need footage where the person is genuinely absent in
# between (see BLOCKERS.md B-4).
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

GAP="${1:-5}"
case "$GAP" in
  ''|*[!0-9.]*) echo "[BLOCKER] gap must be a number of seconds (or 'stop'), got: $GAP"; exit 2 ;;
esac

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

echo "[rtsp] publishing cam1"
nohup $PY scripts/rtsp_stream.py "$VID1" rtsp://127.0.0.1:8554/cam1 \
  > scripts/rtsp_cam1.log 2>&1 &
P1=$!

if [ "$GAP" != "0" ]; then
  echo "[rtsp] waiting ${GAP}s before cam2 (cam2 will lag cam1 by ${GAP}s)"
  sleep "$GAP"
fi

echo "[rtsp] publishing cam2"
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

[rtsp] streams live (cam2 lags cam1 by ${GAP}s):
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
