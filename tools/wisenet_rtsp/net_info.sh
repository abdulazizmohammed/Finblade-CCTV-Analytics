#!/usr/bin/env bash
# Report which address to point the FinBlade CCTV app at.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"

WSL="$(wsl_ip)"
GW="$(host_gateway_ip || true)"
LAN="$(host_lan_ip || true)"

rule
say "WiseNET RTSP — network addresses"
rule
say ""
say "WSL IP:"
say "$WSL"
say ""
say "Windows/host-accessible address if applicable:"
say "${LAN:-unknown}   (Windows LAN address)"
say "${GW:-unknown}   (Windows side of the WSL virtual switch)"
say ""
say "RTSP Port:"
say "$RTSP_PORT"
say ""
rule
say "Which URL should you use?"
rule
say ""
say "FinBlade app running INSIDE this same WSL instance:"
say "  rtsp://127.0.0.1:$RTSP_PORT/set_2/cam_01"
say ""
say "FinBlade app running on the WINDOWS host (or in Docker Desktop on it):"
say "  rtsp://$WSL:$RTSP_PORT/set_2/cam_01"
say ""
say "FinBlade app on ANOTHER machine on the LAN:"
if [ -n "$LAN" ]; then
  say "  rtsp://$LAN:$RTSP_PORT/set_2/cam_01"
else
  say "  rtsp://<windows-lan-ip>:$RTSP_PORT/set_2/cam_01"
fi
say ""
say "  NOTE: WSL2 is NAT'd, so the WSL IP is not reachable from outside this"
say "  PC. To expose it on the LAN, run ONCE in an elevated Windows PowerShell:"
say ""
say "    netsh interface portproxy add v4tov4 listenport=$RTSP_PORT \\"
say "      listenaddress=0.0.0.0 connectport=$RTSP_PORT connectaddress=$WSL"
say "    New-NetFirewallRule -DisplayName 'WiseNET RTSP test' \\"
say "      -Direction Inbound -LocalPort $RTSP_PORT -Protocol TCP -Action Allow"
say ""
say "  Remove it again with:"
say "    netsh interface portproxy delete v4tov4 listenport=$RTSP_PORT listenaddress=0.0.0.0"
say ""
say "  (The WSL IP changes when WSL restarts — re-run net_info.sh after a reboot.)"
