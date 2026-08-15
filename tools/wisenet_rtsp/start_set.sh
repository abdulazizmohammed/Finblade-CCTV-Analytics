#!/usr/bin/env bash
# Start every camera of one WiseNET set as a simultaneous RTSP stream.
#
#   ./start_set.sh 2           play the set once, then stop (default)
#   ./start_set.sh 2 --loop    play continuously
#
# Only one set runs at a time; starting a set stops whatever was running.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"

usage() { say "usage: $(basename "$0") <set-number> [--loop]"; exit 2; }

[ $# -ge 1 ] || usage
N="$1"; shift
LOOP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --loop) LOOP=1 ;;
    -h|--help) usage ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

require_tools
valid_set "$N" || die "no such set: set_$N  (try ./list_sets.sh)"
ensure_mapping

# --- 1. make sure the RTSP server is up -------------------------------------
if ! server_running || ! rtsp_port_open; then
  info "RTSP server is not running — starting it."
  "$ROOT/start_rtsp_server.sh" >/dev/null || die "could not start RTSP server"
fi

# --- 2. only one scenario at a time -----------------------------------------
for other in $(running_sets); do
  info "Set $other is already running — stopping it first."
  "$ROOT/stop_set.sh" "$other" >/dev/null
done

# --- 3. build the camera list ------------------------------------------------
mapfile -t CAMS < <(q cams "$N")
[ "${#CAMS[@]}" -gt 0 ] || die "set_$N contains no .avi files"

mkdir -p "$PIDDIR/set_$N"
rm -f "$PIDDIR/set_$N"/*.pid

BARRIER="$RUNTIME/.start_barrier"
rm -f "$BARRIER"

MODE="one-shot (plays once, then finishes)"
[ "$LOOP" -eq 1 ] && MODE="looping (continuous)"

rule
say "STARTING WiseNET SET $N  —  ${#CAMS[@]} cameras"
say "Mode: $MODE"
rule
say ""

# --- 4. spawn every publisher, all blocked on a shared barrier ---------------
# Each ffmpeg is parked until the barrier file appears, so they all begin
# within a few milliseconds of each other rather than drifting apart as they
# are spawned one by one. No per-camera delays are ever added.
for row in "${CAMS[@]}"; do
  IFS=$'\t' read -r CAM SRC FPS W H CODEC DUR NAME <<< "$row"

  FPS_I=$(printf '%.0f' "${FPS:-25}")
  [ "$FPS_I" -gt 0 ] 2>/dev/null || FPS_I=25
  GOP=$((FPS_I * 2))

  URL="rtsp://127.0.0.1:$RTSP_PORT/set_${N}/${CAM}"
  LOG="$RUNTIME/set_${N}_${CAM}.log"
  : > "$LOG"

  LOOP_OPT=()
  [ "$LOOP" -eq 1 ] && LOOP_OPT=(-stream_loop -1)

  (
    # Ignore SIGHUP so the stream keeps running after this script returns and
    # after the launching terminal is closed. An ignored disposition survives
    # exec, so ffmpeg itself inherits it (this is what nohup does).
    trap '' HUP
    while [ ! -e "$BARRIER" ]; do sleep 0.01; done
    # setsid puts the publisher in its own session so it is fully detached
    # from the terminal that launched it. It execs directly (no fork) here,
    # so the PID recorded below stays correct; reconcile_pid re-checks anyway.
    exec setsid "$FFMPEG" -hide_banner -loglevel warning -nostdin \
      -re "${LOOP_OPT[@]}" -i "$SRC" \
      -an \
      -c:v libx264 -preset ultrafast -tune zerolatency \
      -pix_fmt yuv420p -crf 23 \
      -g "$GOP" -keyint_min "$FPS_I" -sc_threshold 0 \
      -threads 2 \
      -f rtsp -rtsp_transport tcp "$URL"
  # stdin comes from /dev/null so the publisher holds no handle on the
  # terminal that launched it — otherwise the calling shell can block on exit.
  ) </dev/null >"$LOG" 2>&1 &

  # `exec` above replaces the subshell, so $! is the real ffmpeg PID.
  echo $! > "$PIDDIR/set_$N/$CAM.pid"

  printf '  %-8s %-18s %sx%s @ %sfps  %ss\n' \
    "$CAM" "$NAME" "$W" "$H" "$FPS" "$DUR"
done

# Give every subshell time to reach the barrier, then release them together.
sleep 0.5
date +%s > "$PIDDIR/set_$N/.started"
touch "$BARRIER"

echo "$N" > "$ACTIVE_SET_FILE"

# Safety net: if setsid ever forks instead of exec'ing, the PID we recorded
# would be wrong and stop_set.sh could not find the stream. Re-derive each PID
# from the unique RTSP URL in its command line and correct the file.
sleep 0.7
for row in "${CAMS[@]}"; do
  IFS=$'\t' read -r CAM _ <<< "$row"
  PF="$PIDDIR/set_$N/$CAM.pid"
  URL="rtsp://127.0.0.1:$RTSP_PORT/set_${N}/${CAM}"
  PID="$(cat "$PF" 2>/dev/null || true)"
  if [ -z "$PID" ] || ! grep -qa -- "$URL" "/proc/$PID/cmdline" 2>/dev/null; then
    REAL="$(pgrep -f -- "$URL" 2>/dev/null | head -1 || true)"
    [ -n "$REAL" ] && echo "$REAL" > "$PF"
  fi
done

# --- 5. confirm the publishers came up --------------------------------------
say ""
info "Waiting for streams to publish ..."
ok=0
for _ in $(seq 1 40); do
  ok=$(curl -s --max-time 2 "$MTX_API/v3/paths/list" 2>/dev/null \
       | tr ',' '\n' | grep -c "\"set_${N}/cam_" || true)
  [ "$ok" -ge "${#CAMS[@]}" ] && break
  sleep 0.25
done

live=$(set_live_pids "$N" | wc -l)
say ""
if [ "$live" -eq "${#CAMS[@]}" ]; then
  info "SET $N RUNNING — $live/$live cameras publishing"
else
  warn "SET $N started with $live/${#CAMS[@]} cameras alive — check runtime/set_${N}_*.log"
fi
say ""
say "URLs:    ./list_urls.sh $N"
say "Status:  ./status.sh"
say "Stop:    ./stop_set.sh $N"
