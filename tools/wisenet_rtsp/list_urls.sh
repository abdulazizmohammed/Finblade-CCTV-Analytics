#!/usr/bin/env bash
# Print the RTSP URLs for one set, or for every set.
#
#   ./list_urls.sh 2      URLs for set 2
#   ./list_urls.sh        URLs for all sets
#
# Use --ip <addr> to force a specific address in the printed URLs.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"

require_tools
ensure_mapping

IP=""
TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ip) IP="${2:-}"; shift 2 ;;
    -h|--help) say "usage: $(basename "$0") [set-number] [--ip <addr>]"; exit 0 ;;
    *) TARGET="$1"; shift ;;
  esac
done

[ -n "$IP" ] || IP="$(wsl_ip)"

print_set() {
  local n="$1"
  rule
  say "WiseNET SET $n"
  rule
  say ""
  while IFS=$'\t' read -r cam src fps w h codec dur name; do
    say "$(printf '%s' "$cam" | tr 'a-z_' 'A-Z-')"
    say "Source:"
    say "$name"
    say ""
    say "RTSP:"
    say "rtsp://$IP:$RTSP_PORT/set_$n/$cam"
    say ""
    say ""
  done < <(q cams "$n")
}

if [ -n "$TARGET" ]; then
  valid_set "$TARGET" || die "no such set: set_$TARGET"
  print_set "$TARGET"
else
  while read -r n; do print_set "$n"; done < <(q setnums)
fi

say "If the FinBlade app runs inside this same WSL instance, you may also use"
say "rtsp://127.0.0.1:$RTSP_PORT/... instead of rtsp://$IP:$RTSP_PORT/..."
