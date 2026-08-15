#!/usr/bin/env bash
# End-to-end verification of one WiseNET set.
#
#   ./test_set.sh 2            start set 2 and verify every camera
#   ./test_set.sh 2 --wait     also wait for one-shot playback to finish
#
# Checks: server up, every path publishing, every URL reachable over RTSP,
# frames actually decoding, resolution/fps correct, cameras starting together,
# and playback running at real-time speed.
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"

[ $# -ge 1 ] || { say "usage: $(basename "$0") <set-number> [--wait]"; exit 2; }
N="$1"; shift
WAIT=0
[ "${1:-}" = "--wait" ] && WAIT=1

require_tools
valid_set "$N" || die "no such set: set_$N"
ensure_mapping

PASS=0; FAIL=0
ok()   { printf '  [PASS] %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }

rule; say "VERIFYING WiseNET SET $N"; rule

mapfile -t CAMS < <(q cams "$N")
NCAM="${#CAMS[@]}"
say ""; say "Cameras in set: $NCAM"; say ""

# --- 1. RTSP server ---------------------------------------------------------
say "1. RTSP server"
"$ROOT/start_rtsp_server.sh" >/dev/null 2>&1 || true
if server_running && rtsp_port_open; then
  ok "MediaMTX running, port $RTSP_PORT accepting connections"
else
  bad "MediaMTX not running / port $RTSP_PORT closed"
  exit 1
fi

# --- 2. synchronized start --------------------------------------------------
say ""; say "2. Starting set $N (measuring start skew)"
"$ROOT/stop_all_sets.sh" >/dev/null 2>&1 || true

SKEWFILE="$RUNTIME/.test_skew"
( python3 "$LIB/watch_ready.py" "$N" "$NCAM" 60 > "$SKEWFILE" 2>/dev/null ) &
WATCHER=$!
sleep 0.3

"$ROOT/start_set.sh" "$N" >"$RUNTIME/.test_start.log" 2>&1
START_RC=$?
wait "$WATCHER" 2>/dev/null || true

if [ "$START_RC" -eq 0 ]; then ok "start_set.sh completed"; else bad "start_set.sh exited $START_RC"; fi

READY_COUNT="$(awk -F'\t' '$1=="COUNT"{print $2}' "$SKEWFILE" 2>/dev/null || echo 0)"
SKEW_MS="$(awk -F'\t' '$1=="SKEW_MS"{print $2}' "$SKEWFILE" 2>/dev/null || echo -1)"

if [ "${READY_COUNT:-0}" -eq "$NCAM" ]; then
  ok "all $NCAM paths began publishing"
else
  bad "only ${READY_COUNT:-0}/$NCAM paths began publishing"
fi

if [ "${SKEW_MS:-99999}" -ge 0 ] 2>/dev/null && [ "${SKEW_MS%.*}" -le 2000 ] 2>/dev/null; then
  ok "cameras started together — spread ${SKEW_MS} ms across $NCAM streams"
else
  bad "start skew ${SKEW_MS} ms is larger than 2000 ms"
fi
say ""
say "  per-camera start offset (ms after the first camera):"
grep -v -E '^(SKEW_MS|COUNT)' "$SKEWFILE" 2>/dev/null | sed 's/^/    /' || true

# --- 3. every URL reachable and delivering frames ---------------------------
say ""; say "3. Per-camera RTSP verification"
for row in "${CAMS[@]}"; do
  IFS=$'\t' read -r CAM SRC FPS W H CODEC DUR NAME <<< "$row"
  URL="rtsp://127.0.0.1:$RTSP_PORT/set_$N/$CAM"

  PROBE="$("$FFPROBE" -v error -rtsp_transport tcp \
            -select_streams v:0 \
            -show_entries stream=codec_name,width,height \
            -of csv=p=0 -timeout 10000000 "$URL" 2>/dev/null | head -1)"

  if [ -z "$PROBE" ]; then
    bad "$CAM  RTSP not reachable  ($URL)"
    continue
  fi

  P_CODEC="$(printf '%s' "$PROBE" | cut -d, -f1)"
  P_W="$(printf '%s' "$PROBE" | cut -d, -f2)"
  P_H="$(printf '%s' "$PROBE" | cut -d, -f3)"

  # Pull real frames off the wire to prove video is actually flowing.
  FRAMES="$("$FFMPEG" -hide_banner -nostdin -rtsp_transport tcp -i "$URL" \
              -frames:v 25 -f null - 2>&1 | grep -oE 'frame= *[0-9]+' \
              | tail -1 | grep -oE '[0-9]+' || echo 0)"

  if [ "$P_W" = "$W" ] && [ "$P_H" = "$H" ] && [ "${FRAMES:-0}" -ge 20 ]; then
    ok "$CAM  $P_CODEC ${P_W}x${P_H}  ${FRAMES} frames decoded  <- $NAME"
  else
    bad "$CAM  expected ${W}x${H}, got ${P_W}x${P_H}, frames=${FRAMES}  <- $NAME"
  fi
done

# --- 4. real-time pacing ----------------------------------------------------
say ""; say "4. Real-time playback speed (cam_01)"
IFS=$'\t' read -r _ _ FPS1 _ _ _ _ _ <<< "${CAMS[0]}"
FPS1_I="$(printf '%.0f' "${FPS1:-25}")"
NFRAMES=$((FPS1_I * 4))
EXPECTED=4

T0="$(date +%s.%N)"
GOT="$("$FFMPEG" -hide_banner -nostdin -rtsp_transport tcp \
        -i "rtsp://127.0.0.1:$RTSP_PORT/set_$N/cam_01" \
        -frames:v "$NFRAMES" -f null - 2>&1 \
        | grep -oE 'frame= *[0-9]+' | tail -1 | grep -oE '[0-9]+' || echo 0)"
T1="$(date +%s.%N)"
ELAPSED="$(python3 -c "print(round($T1-$T0,2))")"

VERDICT="$(python3 -c "
e=$ELAPSED; x=$EXPECTED
print('OK' if x*0.6 <= e <= x*2.0 else 'BAD')")"

if [ "$VERDICT" = "OK" ] && [ "${GOT:-0}" -ge $((NFRAMES - 5)) ]; then
  ok "read $GOT frames at ${FPS1_I}fps in ${ELAPSED}s (expected ~${EXPECTED}s) — real-time"
else
  bad "read $GOT/$NFRAMES frames in ${ELAPSED}s (expected ~${EXPECTED}s) — not real-time"
fi

# --- 5. one-shot completion -------------------------------------------------
if [ "$WAIT" -eq 1 ]; then
  say ""; say "5. One-shot completion (waiting for playback to end)"
  LONGEST="$(printf '%s\n' "${CAMS[@]}" | awk -F'\t' '{if($7+0>m)m=$7+0}END{print int(m)+20}')"
  say "  longest clip ${LONGEST}s — waiting up to that long ..."
  T0="$(date +%s)"
  while [ $(( $(date +%s) - T0 )) -lt "$LONGEST" ]; do
    [ -z "$(set_live_pids "$N")" ] && break
    sleep 2
  done
  RAN=$(( $(date +%s) - T0 ))
  if [ -z "$(set_live_pids "$N")" ]; then
    ok "all streams finished on their own after ~${RAN}s (one-shot works)"
  else
    bad "streams still running after ${RAN}s — one-shot did not terminate"
  fi
  "$ROOT/stop_set.sh" "$N" >/dev/null 2>&1 || true
fi

# --- summary ----------------------------------------------------------------
say ""; rule
say "RESULT: $PASS passed, $FAIL failed"
rule
[ "$FAIL" -eq 0 ]
