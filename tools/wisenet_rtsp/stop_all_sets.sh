#!/usr/bin/env bash
# Stop every WiseNET test stream. Leaves the RTSP server itself running.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"

mapfile -t SETS < <(running_sets)

if [ "${#SETS[@]}" -eq 0 ]; then
  info "No WiseNET sets are running."
  rm -f "$ACTIVE_SET_FILE"
  exit 0
fi

for n in "${SETS[@]}"; do
  "$ROOT/stop_set.sh" "$n"
done

rm -f "$ACTIVE_SET_FILE"
info "All WiseNET sets stopped. (RTSP server still running — ./stop_rtsp_server.sh to stop it.)"
