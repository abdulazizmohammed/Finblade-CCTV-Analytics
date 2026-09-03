#!/usr/bin/env bash
#
# Publish any local video file as an RTSP stream.
#
#   scripts/rtsp_publish.sh /path/to/video.mp4              # -> rtsp://127.0.0.1:8554/video
#   scripts/rtsp_publish.sh clip.mp4 lobby                  # -> rtsp://127.0.0.1:8554/lobby
#   scripts/rtsp_publish.sh a.mp4 cam1                      # several at once: run it
#   scripts/rtsp_publish.sh b.mp4 cam2                      #   again, the server is shared
#   scripts/rtsp_publish.sh --status
#   scripts/rtsp_publish.sh --stop lobby                    # stop one publisher
#   scripts/rtsp_publish.sh --stop-all                      # publishers + server
#
# Options:
#   --transcode     re-encode to H.264 instead of copying the original stream.
#                   Only needed when the source codec cannot ride RTSP or the
#                   consumer cannot decode it. Costs real CPU - see below.
#   --no-loop       play once and exit instead of looping forever
#   --port N        RTSP port (default 8554)
#
# WHY -c:v copy IS THE DEFAULT. Re-encoding a 1280x720 stream costs a core per
# camera and changes the pixels the detector sees, which quietly invalidates any
# accuracy measurement taken against the original file. Copying remuxes the
# existing frames and costs almost nothing. Fall back to --transcode only when
# copy actually fails, and know that you are then measuring a different video.
#
# WHY IT REUSES A RUNNING SERVER. A second MediaMTX on the same port would fail
# to bind and leave you with a half-broken rig. If something is already serving
# 8554 this publishes an additional path into it, so the streams already running
# (the WiseNET bank, say) keep working and yours joins them.
#
# NOT A CAMERA. A looping file has no wall clock: every loop replays the same
# people at the same timestamps. Cross-camera identity and any rule with a time
# window will behave differently than on live footage. Fine for pipeline and
# throughput work, misleading for accuracy claims.
set -uo pipefail

PORT=8554
TRANSCODE=0
LOOP=1
ACTION="publish"
TARGET=""
RUN_DIR="${XDG_RUNTIME_DIR:-/tmp}/fb-rtsp"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------- args ---
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --transcode) TRANSCODE=1 ;;
    --no-loop)   LOOP=0 ;;
    --port)      PORT="$2"; shift ;;
    --port=*)    PORT="${1#*=}" ;;
    --status)    ACTION="status" ;;
    --stop-all)  ACTION="stop-all" ;;
    --stop)      ACTION="stop"; TARGET="${2:-}"; shift ;;
    -h|--help)   sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)          echo "unknown option: $1" >&2; exit 2 ;;
    *)           ARGS+=("$1") ;;
  esac
  shift
done

mkdir -p "$RUN_DIR"

# ------------------------------------------------------- find mediamtx ---
# Deliberately searched rather than hard-coded: this box has one binary under
# ~/wisenet-rtsp and the repo expects another under scripts/bin that a fresh
# clone never has. Hard-coding either would break on the other machine.
find_mediamtx() {
  local c
  for c in "$REPO/scripts/bin/mediamtx" \
           "$HOME/wisenet-rtsp/mediamtx" \
           "$(command -v mediamtx 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

server_up() { ss -ltn 2>/dev/null | grep -q ":$PORT[[:space:]]"; }

start_server() {
  if server_up; then
    echo "[rtsp] server already listening on :$PORT - reusing it"
    return 0
  fi
  local bin cfg
  bin="$(find_mediamtx)" || {
    cat >&2 <<EOF
[BLOCKER] MediaMTX not found. Looked in:
    $REPO/scripts/bin/mediamtx
    $HOME/wisenet-rtsp/mediamtx
    \$PATH
  Fetch it with: bash scripts/rtsp_setup.sh
EOF
    exit 1
  }
  # Prefer a config next to the binary; fall back to the repo's.
  cfg="$(dirname "$bin")/mediamtx.yml"
  [ -f "$cfg" ] || cfg="$REPO/scripts/mediamtx.yml"
  echo "[rtsp] starting MediaMTX ($bin) on :$PORT"
  nohup "$bin" "$cfg" > "$RUN_DIR/mediamtx.log" 2>&1 &
  echo $! > "$RUN_DIR/mediamtx.pid"
  for _ in $(seq 1 25); do server_up && break; sleep 0.2; done
  server_up || { echo "[BLOCKER] server did not come up; see $RUN_DIR/mediamtx.log" >&2
                 tail -20 "$RUN_DIR/mediamtx.log" >&2; exit 1; }
}

# ------------------------------------------------------------ actions ---
case "$ACTION" in
  status)
    echo "=== server ==="
    if server_up; then
      echo "  listening on :$PORT"
      ps -eo pid,etime,cmd | grep '[m]ediamtx' | sed 's/^/  /'
    else
      echo "  not running"
    fi
    echo "=== publishers ==="
    shopt -s nullglob
    local_found=0
    for f in "$RUN_DIR"/pub-*.pid; do
      pid="$(cat "$f" 2>/dev/null)"
      name="$(basename "$f" .pid)"; name="${name#pub-}"
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "  $name  pid=$pid  rtsp://127.0.0.1:$PORT/$name"
        local_found=1
      else
        rm -f "$f"
      fi
    done
    [ "$local_found" = 0 ] && echo "  none started by this script"
    echo "=== all ffmpeg publishers on this box ==="
    ps -eo pid,cmd | grep '[f]fmpeg.*rtsp://' | sed 's/^/  /' || echo "  none"
    exit 0
    ;;
  stop)
    [ -n "$TARGET" ] || { echo "usage: $0 --stop <stream-name>" >&2; exit 2; }
    f="$RUN_DIR/pub-$TARGET.pid"
    [ -f "$f" ] || { echo "no publisher named '$TARGET' started by this script" >&2; exit 1; }
    kill "$(cat "$f")" 2>/dev/null && echo "[rtsp] stopped $TARGET"
    rm -f "$f"
    exit 0
    ;;
  stop-all)
    shopt -s nullglob
    for f in "$RUN_DIR"/pub-*.pid; do
      kill "$(cat "$f")" 2>/dev/null
      rm -f "$f"
    done
    if [ -f "$RUN_DIR/mediamtx.pid" ]; then
      kill "$(cat "$RUN_DIR/mediamtx.pid")" 2>/dev/null
      rm -f "$RUN_DIR/mediamtx.pid"
      echo "[rtsp] stopped the server this script started"
    else
      echo "[rtsp] server was not started by this script - left running"
    fi
    echo "[rtsp] publishers stopped"
    exit 0
    ;;
esac

# ------------------------------------------------------------ publish ---
VIDEO="${ARGS[0]:-}"
NAME="${ARGS[1]:-}"

if [ -z "$VIDEO" ]; then
  echo "usage: $0 <video-file> [stream-name] [--transcode] [--no-loop]" >&2
  echo "       $0 --status | --stop <name> | --stop-all" >&2
  exit 2
fi
[ -f "$VIDEO" ] || { echo "[BLOCKER] no such file: $VIDEO" >&2; exit 1; }
VIDEO="$(cd "$(dirname "$VIDEO")" && pwd)/$(basename "$VIDEO")"   # absolutise

command -v ffmpeg >/dev/null || { echo "[BLOCKER] ffmpeg not installed" >&2; exit 1; }

# Default the stream name to the filename, sanitised: an RTSP path with a space
# or a slash in it is accepted at publish time and then fails to resolve when
# something tries to read it, which is a confusing way to lose an hour.
if [ -z "$NAME" ]; then
  NAME="$(basename "${VIDEO%.*}")"
  NAME="$(echo "$NAME" | tr -c '[:alnum:]_-' '_' | sed 's/_\+/_/g; s/^_//; s/_$//')"
fi

start_server

URL="rtsp://127.0.0.1:$PORT/$NAME"
LOG="$RUN_DIR/pub-$NAME.log"
PIDF="$RUN_DIR/pub-$NAME.pid"

if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "[rtsp] '$NAME' is already publishing (pid $(cat "$PIDF")). Stop it first:"
  echo "       $0 --stop $NAME"
  exit 1
fi

# Report what the source actually is, so a copy-vs-transcode decision is made
# on evidence rather than on the file extension.
CODEC="$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height \
         -of csv=p=0 "$VIDEO" 2>/dev/null)"
echo "[rtsp] source : $VIDEO"
echo "[rtsp] stream : $CODEC"

FF=(ffmpeg -nostdin -hide_banner -loglevel error -re)
[ "$LOOP" = 1 ] && FF+=(-stream_loop -1)
FF+=(-i "$VIDEO" -an)
if [ "$TRANSCODE" = 1 ]; then
  FF+=(-c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p)
  echo "[rtsp] mode   : TRANSCODE to H.264 (costs a core; pixels differ from the source)"
else
  FF+=(-c:v copy)
  echo "[rtsp] mode   : copy (no re-encode)"
fi
FF+=(-f rtsp -rtsp_transport tcp "$URL")

nohup "${FF[@]}" > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PIDF"

# Give it a moment, then verify it is actually serving rather than reporting
# success on a process that died half a second later - the usual failure is a
# codec that cannot ride RTSP, and it exits immediately.
sleep 3
if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PIDF"
  echo "[BLOCKER] publisher exited immediately. ffmpeg said:" >&2
  tail -8 "$LOG" >&2
  [ "$TRANSCODE" = 0 ] && echo "  Try again with --transcode: the source codec may not ride RTSP." >&2
  exit 1
fi

if timeout 20 ffprobe -v error -rtsp_transport tcp \
     -show_entries stream=codec_name,width,height -of csv=p=0 "$URL" >/dev/null 2>&1; then
  echo "[rtsp] LIVE   : $URL"
else
  echo "[rtsp] started (pid $PID) but the stream did not answer a probe yet."
  echo "        It may still be warming up. Check: $LOG"
fi

cat <<EOF

  Add it as a camera:  http://localhost:8000/web/cameras.html
    source: $URL

  Stop it:             $0 --stop $NAME
  Everything:          $0 --status
EOF
