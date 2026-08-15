#!/usr/bin/env bash
# Start the MediaMTX RTSP server used by the WiseNET test streams.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"

require_tools

if server_running; then
  info "RTSP server already running (pid $(server_pid), port $RTSP_PORT)"
  exit 0
fi

# Render the active config with an absolute runtime path for the log file.
sed "s#__RUNTIME__#$RUNTIME#g" "$MTX_TEMPLATE" > "$MTX_ACTIVE"

info "Starting MediaMTX on port $RTSP_PORT ..."
: > "$MTX_LOG"
nohup "$MTX_BIN" "$MTX_ACTIVE" </dev/null >"$RUNTIME/mediamtx.stdout.log" 2>&1 &
echo $! > "$MTX_PIDFILE"

# Wait for the RTSP port to accept connections.
for _ in $(seq 1 50); do
  if rtsp_port_open; then
    info "RTSP server RUNNING (pid $(cat "$MTX_PIDFILE"))"
    say ""
    say "  rtsp://127.0.0.1:$RTSP_PORT/<set>/<cam>          (same WSL instance)"
    say "  rtsp://$(wsl_ip):$RTSP_PORT/<set>/<cam>   (from Windows host)"
    say ""
    say "Next:  ./list_sets.sh   then   ./start_set.sh 2"
    exit 0
  fi
  sleep 0.2
done

warn "RTSP server did not open port $RTSP_PORT within 10s. Last log lines:"
tail -n 20 "$MTX_LOG" "$RUNTIME/mediamtx.stdout.log" 2>/dev/null >&2 || true
rm -f "$MTX_PIDFILE"
exit 1
