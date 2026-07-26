#!/usr/bin/env bash
# Serve a local video file as an RTSP stream, so a camera can be pointed at an
# RTSP source (rtsp://…) instead of a file — for testing the real network path.
#
#   scripts/rtsp_stream.sh <video-file> [cam01|cam02|<path>]
#
# Starts the MediaMTX RTSP server (once) and publishes the clip (looping) to
# rtsp://127.0.0.1:8554/<path>. Ctrl-C stops publishing. Run scripts/rtsp_setup.sh
# first if MediaMTX / PyAV aren't installed yet.
set -u
cd "$(dirname "$0")/.."

VIDEO="${1:-}"
CAM="${2:-cam01}"
if [ -z "$VIDEO" ]; then
  echo "usage: scripts/rtsp_stream.sh <video-file> [cam01|cam02|<path>]"; exit 2
fi
[ -f "$VIDEO" ] || { echo "no such file: $VIDEO"; exit 1; }
[ -x scripts/bin/mediamtx ] || { echo "MediaMTX missing — run: bash scripts/rtsp_setup.sh"; exit 1; }

URL="rtsp://127.0.0.1:8554/${CAM}"

# start the RTSP server once (shared by cam01 + cam02)
if ! pgrep -f 'scripts/bin/mediamtx' >/dev/null 2>&1; then
  echo "[rtsp] starting MediaMTX server on :8554"
  nohup scripts/bin/mediamtx scripts/mediamtx.yml > scripts/mediamtx.log 2>&1 &
  sleep 2
fi

echo "[rtsp] publishing '$VIDEO'  ->  $URL   (Ctrl-C to stop)"
echo "[rtsp] point a camera at it, e.g.:"
if [ "$CAM" = "cam02" ]; then
  echo "  .venv/bin/python services/inference/run_cpu.py --config config/cameras.cam2.yaml \\"
  echo "     --source $URL --port 8081 --stream-host localhost --api-url http://127.0.0.1:8000"
else
  echo "  .venv/bin/python services/inference/run_cpu.py --config config/cameras.synthetic.yaml \\"
  echo "     --source $URL --port 8080 --stream-host localhost --api-url http://127.0.0.1:8000"
fi
exec .venv/bin/python scripts/rtsp_stream.py "$VIDEO" "$URL"
