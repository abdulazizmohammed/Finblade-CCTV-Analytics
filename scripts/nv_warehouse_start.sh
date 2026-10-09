#!/usr/bin/env bash
#
# Bring up the six-camera warehouse bank (CAM-NVW-00..05) end to end.
#
#   bash scripts/nv_warehouse_start.sh            # publish + API + register + workers
#   bash scripts/nv_warehouse_start.sh --status
#   bash scripts/nv_warehouse_start.sh --stop     # workers + publishers (API left up)
#
# Expects the dataset copied (gitignored) to media/nv_warehouse/raw/cam_0{0..5}.mp4.
#
# WHY THE PUBLISHERS START TOGETHER. The six recordings are synchronised (9000
# frames each, same 300 s). Cross-camera identity treats simultaneous presence
# on overlapping cameras as evidence FOR a match, within overlap_tolerance (5 s).
# scripts/rtsp_publish.sh probes each stream for up to ~23 s before returning,
# so six sequential calls would skew the cameras by tens of seconds. Here every
# ffmpeg waits on one barrier file and starts within milliseconds of the others,
# the same technique as tools/wisenet_rtsp/start_set.sh.
#
# LOOPING. Every file is exactly 9000 frames, so the loops stay aligned to
# within -re pacing drift. A loop still replays the same people at new wall-clock
# times: identity and any time-window rule behave differently than on live
# footage. Fine for a demo, not for accuracy claims.
#
# Streams are -c:v copy (MPEG-4 Part 2 over RTSP, verified readable by OpenCV),
# so the detector sees the source pixels.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[ -f .env ] && { set -a; . ./.env; set +a; }

PY=.venv/bin/python
API=http://127.0.0.1:8000
PORT=8554
SITE=RUH-WH1
CFG=config/cameras.nvw.yaml
RAW=media/nv_warehouse/raw
RUN_DIR="${XDG_RUNTIME_DIR:-/tmp}/fb-rtsp"     # shared with rtsp_publish.sh --status
IDX=(0 1 2 3 4 5)
MJPEG_BASE=8190                                  # clear of camera_manager's 8090+
mkdir -p "$RUN_DIR"

AUTH=()
[ -n "${FINBLADE_API_KEY:-}" ] && AUTH=(-H "Authorization: Bearer ${FINBLADE_API_KEY}")

server_up() { ss -ltn 2>/dev/null | grep -q ":$PORT[[:space:]]"; }
api_up()    { curl -sf "${AUTH[@]}" "$API/api/v1/identity/stats" >/dev/null 2>&1; }

stop_bank() {
  for i in "${IDX[@]}"; do
    for f in "$RUN_DIR/nvw-worker-$i.pid" "$RUN_DIR/pub-nvw_cam0$i.pid"; do
      [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null
      rm -f "$f"
    done
  done
  echo "[nvw] workers and publishers stopped (API and MediaMTX left running)"
}

status() {
  echo "=== publishers ==="
  for i in "${IDX[@]}"; do
    p="$(cat "$RUN_DIR/pub-nvw_cam0$i.pid" 2>/dev/null)"
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then s="up   pid=$p"; else s="DOWN"; fi
    echo "  nvw_cam0$i  $s"
  done
  echo "=== workers ==="
  for i in "${IDX[@]}"; do
    p="$(cat "$RUN_DIR/nvw-worker-$i.pid" 2>/dev/null)"
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then s="up   pid=$p"; else s="DOWN"; fi
    echo "  CAM-NVW-0$i  $s  mjpeg http://localhost:$((MJPEG_BASE + i))/  log scripts/cam_CAM-NVW-0$i.log"
  done
}

case "${1:-}" in
  --stop)   stop_bank; exit 0 ;;
  --status) status; exit 0 ;;
  "")       ;;
  *)        echo "usage: $0 [--status|--stop]" >&2; exit 2 ;;
esac

# ---------------------------------------------------------------- preflight ---
for i in "${IDX[@]}"; do
  [ -f "$RAW/cam_0$i.mp4" ] || { echo "[BLOCKER] missing $RAW/cam_0$i.mp4 — copy the dataset first" >&2; exit 1; }
done
[ -n "${DATABASE_URL:-}" ] || { echo "[BLOCKER] DATABASE_URL not set (.env)" >&2; exit 1; }

# ------------------------------------------------------------------ MediaMTX ---
if ! server_up; then
  BIN=""
  for c in "$REPO/scripts/bin/mediamtx" "$HOME/wisenet-rtsp/mediamtx" "$(command -v mediamtx 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] && { BIN="$c"; break; }
  done
  [ -n "$BIN" ] || { echo "[BLOCKER] MediaMTX not found; run: bash scripts/rtsp_setup.sh" >&2; exit 1; }
  MCFG="$(dirname "$BIN")/mediamtx.yml"; [ -f "$MCFG" ] || MCFG="$REPO/scripts/mediamtx.yml"
  echo "[nvw] starting MediaMTX ($BIN)"
  nohup "$BIN" "$MCFG" > "$RUN_DIR/mediamtx.log" 2>&1 &
  echo $! > "$RUN_DIR/mediamtx.pid"
  for _ in $(seq 1 25); do server_up && break; sleep 0.2; done
  server_up || { echo "[BLOCKER] MediaMTX did not come up; see $RUN_DIR/mediamtx.log" >&2; exit 1; }
fi

# -------------------------------------------- publishers, released together ---
stop_bank >/dev/null
BARRIER="$RUN_DIR/nvw.barrier"; rm -f "$BARRIER"
for i in "${IDX[@]}"; do
  URL="rtsp://127.0.0.1:$PORT/nvw_cam0$i"
  (
    trap '' HUP
    while [ ! -e "$BARRIER" ]; do sleep 0.01; done
    exec setsid ffmpeg -nostdin -hide_banner -loglevel error -re -stream_loop -1 \
      -i "$REPO/$RAW/cam_0$i.mp4" -an -c:v copy -f rtsp -rtsp_transport tcp "$URL"
  ) </dev/null > "$RUN_DIR/pub-nvw_cam0$i.log" 2>&1 &
  echo $! > "$RUN_DIR/pub-nvw_cam0$i.pid"
done
sleep 0.5; touch "$BARRIER"; sleep 3
dead=0
for i in "${IDX[@]}"; do
  kill -0 "$(cat "$RUN_DIR/pub-nvw_cam0$i.pid")" 2>/dev/null || { echo "[BLOCKER] publisher nvw_cam0$i exited:" >&2; tail -5 "$RUN_DIR/pub-nvw_cam0$i.log" >&2; dead=1; }
done
[ "$dead" = 0 ] || exit 1
echo "[nvw] 6 RTSP streams live: rtsp://127.0.0.1:$PORT/nvw_cam00..05"

# ----------------------------------------------------------------------- API ---
if api_up; then
  echo "[nvw] API already up on $API"
else
  # Autostart OFF for this launch: the stored RUH-01 cameras point at RTSP paths
  # that are not being published, and each would load the template's six models
  # just to retry a dead source. Start them deliberately if you want them.
  echo "[nvw] starting API (topology=${FINBLADE_TOPOLOGY:-config/topology.yaml}, camera autostart off)"
  FINBLADE_AUTOSTART_CAMERAS=0 nohup $PY -m uvicorn services.api.app:app \
    --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
  for _ in $(seq 1 60); do api_up && break; sleep 0.5; done
  api_up || { echo "[BLOCKER] API did not come up; see scripts/api.log" >&2; tail -20 scripts/api.log >&2; exit 1; }
fi

# ------------------------------------------------------- branch + cameras ---
KEYARG=(); [ -n "${FINBLADE_API_KEY:-}" ] && KEYARG=(--key "$FINBLADE_API_KEY")
$PY scripts/seed_org.py --config config/org.demo-warehouse.yaml --api-url "$API" "${KEYARG[@]}" \
  || { echo "[BLOCKER] org import failed" >&2; exit 1; }

# Registered WITHOUT a source on purpose: with a source, POST /api/v1/cameras
# also launches a worker on the shared template (see config/cameras.nvw.yaml).
for i in "${IDX[@]}"; do
  body="{\"camera_id\":\"CAM-NVW-0$i\",\"site_id\":\"$SITE\",\"name\":\"Warehouse cam $i\"}"
  out="$(curl -s "${AUTH[@]}" -X POST "$API/api/v1/cameras" -H 'Content-Type: application/json' -d "$body")"
  echo "[nvw] register CAM-NVW-0$i -> $out"
done

# ------------------------------------------------------------------ workers ---
for i in "${IDX[@]}"; do
  nohup $PY -u services/inference/run_cpu.py --config "$CFG" \
    --camera-id "CAM-NVW-0$i" --site-id "$SITE" \
    --source "rtsp://127.0.0.1:$PORT/nvw_cam0$i" \
    --port $((MJPEG_BASE + i)) --stream-host localhost --api-url "$API" \
    > "scripts/cam_CAM-NVW-0$i.log" 2>&1 &
  echo $! > "$RUN_DIR/nvw-worker-$i.pid"
done
echo "[nvw] 6 workers launched; models take ~20-40 s to load"
echo
status
