#!/usr/bin/env bash
# Show RTSP server state, network addresses, and any running WiseNET set.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"

rule
say "WiseNET RTSP Test Environment — status"
rule
say ""

# --- server -----------------------------------------------------------------
if server_running && rtsp_port_open; then
  say "RTSP Server:  RUNNING  (pid $(server_pid), port $RTSP_PORT)"
else
  say "RTSP Server:  STOPPED"
fi

say ""
say "WSL IP:       $(wsl_ip)"
gw="$(host_gateway_ip || true)"
say "Windows host: ${gw:-n/a}  (WSL vSwitch gateway)"
say "RTSP Port:    $RTSP_PORT"
say ""

# --- streams ----------------------------------------------------------------
mapfile -t SETS < <(running_sets)

if [ "${#SETS[@]}" -eq 0 ]; then
  say "Running set:  none"
  say ""
  say "Start one with:  ./start_set.sh 2"
  exit 0
fi

for n in "${SETS[@]}"; do
  started="$(cat "$PIDDIR/set_$n/.started" 2>/dev/null || true)"
  elapsed="?"
  if [ -n "$started" ]; then elapsed="$(( $(date +%s) - started ))s"; fi

  say "Running set:  SET $n   (elapsed ${elapsed})"
  say ""
  printf '  %-8s %-8s %-9s %-12s %s\n' CAM PID STATE READERS RTSP
  printf '  %-8s %-8s %-9s %-12s %s\n' ------ ------- -------- ------- ----

  while read -r pid cam; do
    [ -n "${cam:-}" ] || continue
    path="set_${n}/${cam}"
    # Ask MediaMTX whether this path is actually publishing, and to whom.
    js="$(curl -s --max-time 3 "$MTX_API/v3/paths/get/$path" 2>/dev/null || true)"
    state="no-path"
    readers="0"
    if [ -n "$js" ] && printf '%s' "$js" | grep -q '"ready"'; then
      if printf '%s' "$js" | grep -q '"ready": *true'; then state="PUBLISHING"; else state="not-ready"; fi
      readers="$(printf '%s' "$js" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("readers",[])))' 2>/dev/null || echo '?')"
    fi
    printf '  %-8s %-8s %-9s %-12s rtsp://%s:%s/%s\n' \
      "$cam" "$pid" "$state" "$readers" "$(wsl_ip)" "$RTSP_PORT" "$path"
  done < <(set_live_pids "$n")

  say ""
done

say "Logs:  $RUNTIME/set_<n>_cam_<xx>.log"
