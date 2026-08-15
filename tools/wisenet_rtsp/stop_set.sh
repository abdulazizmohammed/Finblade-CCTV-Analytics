#!/usr/bin/env bash
# Stop the RTSP streams of one WiseNET set.
#
# Only kills PIDs this infrastructure recorded AND whose /proc cmdline still
# matches the exact RTSP URL we published to. Never touches other ffmpeg
# processes on the machine.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"

[ $# -ge 1 ] || { say "usage: $(basename "$0") <set-number>"; exit 2; }
N="$1"
valid_set "$N" || die "no such set: set_$N"

mapfile -t LIVE < <(set_live_pids "$N")

if [ "${#LIVE[@]}" -eq 0 ]; then
  info "Set $N is not running."
  rm -f "$PIDDIR/set_$N"/*.pid 2>/dev/null || true
  if [ "$(active_set 2>/dev/null || true)" = "$N" ]; then rm -f "$ACTIVE_SET_FILE"; fi
  exit 0
fi

info "Stopping set $N (${#LIVE[@]} streams) ..."
for row in "${LIVE[@]}"; do
  read -r pid cam <<< "$row"
  printf '  %-8s pid %-7s stopping\n' "$cam" "$pid"
  kill -TERM "$pid" 2>/dev/null || true
done

# Short grace period for SIGTERM.
#
# Measured on this box: ffmpeg 7.0.2 catches SIGTERM and SIGINT but does not
# act on either while it is publishing (it sits in futex_wait_queue and was
# still alive after 15s). Only SIGQUIT ends it, and that dumps core. So we
# fall through to SIGKILL, which is safe here: these are synthetic camera
# feeds with nothing to flush, and MediaMTX drops the path within ~2s of the
# socket closing (verified: 6/6 paths gone 2s after SIGKILL).
for _ in $(seq 1 10); do
  [ -z "$(set_live_pids "$N")" ] && break
  sleep 0.2
done

mapfile -t STILL < <(set_live_pids "$N")
for row in "${STILL[@]:-}"; do
  [ -n "$row" ] || continue
  read -r pid cam <<< "$row"
  kill -KILL "$pid" 2>/dev/null || true
done

# Confirm nothing of ours is left behind.
for _ in $(seq 1 15); do
  [ -z "$(set_live_pids "$N")" ] && break
  sleep 0.2
done
if [ -n "$(set_live_pids "$N")" ]; then
  warn "some publishers of set $N could not be stopped:"
  set_live_pids "$N" >&2
fi

rm -f "$PIDDIR/set_$N"/*.pid "$PIDDIR/set_$N/.started" 2>/dev/null || true
if [ "$(active_set 2>/dev/null || true)" = "$N" ]; then rm -f "$ACTIVE_SET_FILE"; fi

info "Set $N STOPPED"
