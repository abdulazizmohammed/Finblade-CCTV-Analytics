#!/usr/bin/env bash
# Shared configuration and helpers for the WiseNET RTSP test infrastructure.
# Sourced by every script in this directory. Not meant to be run directly.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DATASET="${WISENET_DATASET:-/home/usv/finblade-cctv/media/video_sets}"
BIN="$ROOT/bin"
LIB="$ROOT/lib"
RUNTIME="$ROOT/runtime"
PIDDIR="$RUNTIME/pids"
MAPPING="$ROOT/wisenet_streams.json"

FFMPEG="${WISENET_FFMPEG:-/home/usv/finblade-cctv/.tools/ffmpeg}"
FFPROBE="${WISENET_FFPROBE:-$BIN/ffprobe}"
MTX_BIN="$BIN/mediamtx"
MTX_TEMPLATE="$ROOT/mediamtx.yml"
MTX_ACTIVE="$RUNTIME/mediamtx.active.yml"
MTX_PIDFILE="$RUNTIME/mediamtx.pid"
MTX_LOG="$RUNTIME/mediamtx.log"
MTX_API="http://127.0.0.1:9997"

RTSP_PORT=8554
ACTIVE_SET_FILE="$RUNTIME/active_set"

mkdir -p "$RUNTIME" "$PIDDIR"

# --- output helpers ---------------------------------------------------------
say()  { printf '%s\n' "$*"; }
info() { printf '[*] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[X] %s\n' "$*" >&2; exit 1; }

rule() { printf '%s\n' "=================================================="; }

# --- prerequisite checks ----------------------------------------------------
require_tools() {
  [ -x "$FFMPEG" ]  || die "ffmpeg not found at $FFMPEG"
  [ -x "$FFPROBE" ] || die "ffprobe not found at $FFPROBE"
  [ -x "$MTX_BIN" ] || die "mediamtx not found at $MTX_BIN"
  [ -d "$DATASET" ] || die "dataset not found at $DATASET"
  command -v python3 >/dev/null || die "python3 is required"
}

# --- mapping ----------------------------------------------------------------
# Regenerate wisenet_streams.json if it is missing or older than the dataset.
ensure_mapping() {
  if [ ! -f "$MAPPING" ] || [ "$DATASET" -nt "$MAPPING" ]; then
    info "Scanning dataset (this probes every video once)..."
    "$LIB/scan.py" >/dev/null || die "dataset scan failed"
  fi
}

q() { python3 "$LIB/query.py" "$@"; }

# --- network ----------------------------------------------------------------
# The address of this WSL instance on the Windows<->WSL virtual network.
# Taken from the outbound route, so the loopback-scoped WSL DNS address
# (10.255.255.254/32 on lo) is never picked by mistake.
wsl_ip() {
  local ip
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*[[:space:]]src[[:space:]]\+\([0-9.]\+\).*/\1/p' | head -1)"
  if [ -z "$ip" ]; then
    ip="$(ip -4 -o addr show scope global 2>/dev/null \
          | grep -v ' lo ' | awk '{print $4}' | cut -d/ -f1 | head -1)"
  fi
  printf '%s' "${ip:-127.0.0.1}"
}

# The Windows host as seen from inside WSL (the WSL vSwitch gateway).
host_gateway_ip() {
  ip route 2>/dev/null | awk '/^default/ {print $3; exit}'
}

# The Windows host's LAN address, for machines elsewhere on the network.
# Best effort via Windows interop; empty if unavailable. The answer is cached
# because enumerating adapters on a host with many NICs takes tens of seconds,
# and status.sh should stay instant.
host_lan_ip() {
  local cache="$RUNTIME/host_lan_ip" ps ip
  if [ -s "$cache" ] && [ -z "${WISENET_REFRESH_IP:-}" ]; then
    cat "$cache"; return 0
  fi
  ps="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
  [ -x "$ps" ] || return 0
  ip="$(timeout 60 "$ps" -NoProfile -NonInteractive -Command \
        'Get-NetRoute -DestinationPrefix "0.0.0.0/0" | Sort-Object RouteMetric | Select-Object -First 1 | ForEach-Object { (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress }' \
        2>/dev/null | tr -d '\r' | grep -E '^[0-9.]+$' | head -1)"
  [ -n "$ip" ] && printf '%s' "$ip" > "$cache"
  printf '%s' "$ip"
}

# --- server state -----------------------------------------------------------
server_pid() {
  [ -f "$MTX_PIDFILE" ] || return 1
  local pid
  pid="$(cat "$MTX_PIDFILE" 2>/dev/null)"
  [ -n "$pid" ] || return 1
  # Confirm the PID is really our mediamtx, not a recycled PID.
  grep -qa 'mediamtx' "/proc/$pid/cmdline" 2>/dev/null || return 1
  printf '%s' "$pid"
}

server_running() { server_pid >/dev/null 2>&1; }

rtsp_port_open() {
  timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$RTSP_PORT" 2>/dev/null
}

# --- running-set state ------------------------------------------------------
active_set() {
  [ -f "$ACTIVE_SET_FILE" ] || return 1
  cat "$ACTIVE_SET_FILE" 2>/dev/null
}

# PIDs of live ffmpeg publishers for a set. Verifies each PID's own cmdline
# still refers to this infrastructure before reporting it as ours.
set_live_pids() {
  local n="$1" f pid url
  [ -d "$PIDDIR/set_$n" ] || return 0
  for f in "$PIDDIR/set_$n"/*.pid; do
    [ -e "$f" ] || continue
    pid="$(cat "$f" 2>/dev/null)"
    [ -n "$pid" ] || continue
    url="rtsp://127.0.0.1:$RTSP_PORT/set_${n}/$(basename "$f" .pid)"
    if grep -qa -- "$url" "/proc/$pid/cmdline" 2>/dev/null; then
      printf '%s %s\n' "$pid" "$(basename "$f" .pid)"
    fi
  done
}

set_running() { [ -n "$(set_live_pids "$1")" ]; }

# Any set with live publishers (there should be at most one).
running_sets() {
  local d n
  for d in "$PIDDIR"/set_*; do
    [ -d "$d" ] || continue
    n="${d##*/set_}"
    if set_running "$n"; then printf '%s\n' "$n"; fi
  done
}

valid_set() {
  local n="$1"
  [[ "$n" =~ ^[0-9]+$ ]] || return 1
  [ -d "$DATASET/set_$n" ]
}
