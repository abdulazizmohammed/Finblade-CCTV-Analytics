#!/usr/bin/env bash
# List every WiseNET set with its camera count and video properties.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"

require_tools
ensure_mapping

running="$(running_sets | tr '\n' ' ')"

rule
say "WiseNET video sets"
say "$DATASET"
rule
say ""
printf '  %-6s %-9s %-12s %-7s %-10s %-10s %s\n' \
  SET CAMERAS RESOLUTION FPS DURATION CODEC ''
printf '  %-6s %-9s %-12s %-7s %-10s %-10s %s\n' \
  ------ ------- ---------- ----- -------- -------- ''

while IFS=$'\t' read -r n cams res fps dur codec; do
  mark=""
  case " $running " in *" $n "*) mark="  <-- RUNNING" ;; esac
  printf '  %-6s %-9s %-12s %-7s %-10s %-10s%s\n' \
    "$n" "$cams" "$res" "$fps" "${dur}s" "$codec" "$mark"
done < <(q sets)

say ""
say "Start a set:   ./start_set.sh <n>          (loops continuously)"
say "               ./start_set.sh <n> --once   (plays once, then finishes)"
say "Show URLs:     ./list_urls.sh <n>"
