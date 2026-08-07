#!/usr/bin/env bash
# Serve a local clip as a live RTSP camera, for dev testing.
#
#   scripts/rtsp_demo.sh start     start mediamtx in the background
#   scripts/rtsp_demo.sh status    is it up, and what can you connect to
#   scripts/rtsp_demo.sh verify    actually pull frames and prove it works
#   scripts/rtsp_demo.sh stop      stop it
#
# Streams are runOnDemand: ffmpeg only transcodes while something is connected,
# so `start` looks idle until a camera worker attaches. That is intended, and
# `verify` is what proves the path end to end.
set -u
cd "$(dirname "$0")/.."

CONF="config/mediamtx.demo.yml"
BIN="scripts/bin/mediamtx"
PIDFILE="scripts/logs/mediamtx.pid"
LOG="scripts/logs/mediamtx.log"
mkdir -p scripts/logs

host_ip() {
  # The address a camera worker on this box should dial. 127.0.0.1 is right for
  # a worker in the same WSL instance; the LAN address is what a browser or
  # another machine needs.
  hostname -I 2>/dev/null | awk '{print $1}'
}

running() {
  [ -f "${PIDFILE}" ] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null
}

case "${1:-status}" in
start)
  if running; then echo "already running (pid $(cat "${PIDFILE}"))"; exit 0; fi
  [ -x "${BIN}" ] || { echo "missing ${BIN}"; exit 1; }
  [ -x .tools/ffmpeg ] || { echo "missing .tools/ffmpeg — run scripts/get_ffmpeg.sh"; exit 1; }
  # setsid, not just nohup.
  #
  # nohup only detaches from the terminal; the process stays in the caller's
  # PROCESS GROUP, so anything that signals the group — a killed wrapper, a
  # closed automation session — takes the server with it. setsid puts it in its
  # own session and it survives the shell that launched it.
  setsid nohup "${BIN}" "${CONF}" >"${LOG}" 2>&1 < /dev/null &
  echo $! > "${PIDFILE}"
  for _ in $(seq 20); do
    ss -ltn 2>/dev/null | grep -q ':8554' && break
    sleep 0.5
  done
  if running && ss -ltn 2>/dev/null | grep -q ':8554'; then
    echo "mediamtx started (pid $(cat "${PIDFILE}")), listening on 8554"
  else
    echo "mediamtx failed to start:"; tail -20 "${LOG}"; exit 1
  fi
  ;;

stop)
  if running; then
    kill "$(cat "${PIDFILE}")" 2>/dev/null
    sleep 1
    echo "stopped"
  else
    echo "not running"
  fi
  rm -f "${PIDFILE}"
  ;;

status)
  if running; then echo "mediamtx: running (pid $(cat "${PIDFILE}"))"
  else echo "mediamtx: not running"; fi
  ss -ltn 2>/dev/null | grep -q ':8554' && echo "port 8554: listening" \
                                        || echo "port 8554: closed"
  echo
  echo "RTSP URLs:"
  echo "  rtsp://127.0.0.1:8554/cam-demo        # 1280x960 @15fps (use this one)"
  echo "  rtsp://127.0.0.1:8554/cam-demo-uhd    # 1920x1440 @30fps, heavy"
  ip=$(host_ip)
  [ -n "${ip}" ] && echo "  rtsp://${ip}:8554/cam-demo            # from another machine"
  ;;

verify)
  # Pull real frames with OpenCV — the same decoder the camera worker uses, so
  # this proves the whole path rather than just that a port is open.
  .venv/bin/python - <<'PY'
import time
import cv2
url = "rtsp://127.0.0.1:8554/cam-demo"
print(f"connecting to {url} (ffmpeg starts on demand, allow a few seconds)...")
cap = None
for attempt in range(4):
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if cap.isOpened():
        break
    cap.release()
    time.sleep(3)
if not cap or not cap.isOpened():
    print("FAILED: could not open the stream")
    raise SystemExit(1)

w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
print(f"opened: {w}x{h}")

got, t0 = 0, time.time()
while got < 45 and time.time() - t0 < 25:
    ok, frame = cap.read()
    if ok:
        got += 1
secs = time.time() - t0
cap.release()
print(f"read {got} frames in {secs:.1f}s ({got/secs:.1f} fps)")
if got:
    cv2.imwrite("evidence/rtsp_demo_frame.jpg", frame)
    print("saved evidence/rtsp_demo_frame.jpg — LOOK AT IT to confirm the picture")
print("OK" if got >= 10 else "FAILED: too few frames")
raise SystemExit(0 if got >= 10 else 1)
PY
  ;;

selftest)
  # start -> pull frames -> stop, all inside ONE process.
  #
  # `start` backgrounds mediamtx with nohup, which is right for a terminal you
  # are sitting at but does not survive an automated one-shot invocation: this
  # WSL distro tears the session down when the command returns and takes the
  # server with it. Everything therefore happens here, in one lifetime.
  [ -x "${BIN}" ] || { echo "missing ${BIN}"; exit 1; }
  [ -x .tools/ffmpeg ] || { echo "missing .tools/ffmpeg — run scripts/get_ffmpeg.sh"; exit 1; }
  "${BIN}" "${CONF}" >"${LOG}" 2>&1 &
  MTX=$!
  trap 'kill ${MTX} 2>/dev/null' EXIT
  for _ in $(seq 30); do
    ss -ltn 2>/dev/null | grep -q ':8554' && break
    sleep 0.5
  done
  if ! ss -ltn 2>/dev/null | grep -q ':8554'; then
    echo "mediamtx did not open 8554:"; tail -20 "${LOG}"; exit 1
  fi
  echo "mediamtx listening on 8554"
  echo
  bash "$0" verify        # bash, not "$0": the file need not be +x to be run
  rc=$?
  echo
  echo "--- mediamtx log (last 12 lines)"
  tail -12 "${LOG}"
  exit "${rc}"
  ;;

*)
  echo "usage: $0 {start|stop|status|verify|selftest}"; exit 2 ;;
esac
