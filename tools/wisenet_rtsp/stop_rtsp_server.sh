#!/usr/bin/env bash
# Stop the MediaMTX RTSP server (and any streams still publishing to it).
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"

# Never leave orphaned publishers behind.
if [ -n "$(running_sets)" ]; then
  info "Stopping running stream sets first ..."
  "$ROOT/stop_all_sets.sh"
fi

pid="$(server_pid 2>/dev/null || true)"
if [ -z "$pid" ]; then
  info "RTSP server is not running."
  rm -f "$MTX_PIDFILE"
  exit 0
fi

info "Stopping MediaMTX (pid $pid) ..."
kill -TERM "$pid" 2>/dev/null || true
for _ in $(seq 1 25); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.2
done
if kill -0 "$pid" 2>/dev/null; then
  warn "MediaMTX did not exit on SIGTERM, sending SIGKILL"
  kill -KILL "$pid" 2>/dev/null || true
fi

rm -f "$MTX_PIDFILE"
info "RTSP server STOPPED"
