#!/usr/bin/env bash
# Start the API and one camera pipeline for hands-on testing.
#
#   bash scripts/start_demo.sh            # foreground; Ctrl-C stops the API
#   bash scripts/stop_demo.sh             # stops everything
#
# Keys come from .local_key (operator) and .local_integration_key (the scoped
# key a platform integration would be given). Both are generated on first run
# and are gitignored.
set -u
cd "$(dirname "$0")/.." || exit 1

PORT="${PORT:-8000}"
CAMERA_ID="${CAMERA_ID:-CAM-01}"
SITE_ID="${SITE_ID:-SITE-01}"

# Source resolution, in order:
#   1. CLIP=… or the first argument — a file path OR an rtsp:// URL
#   2. any .mp4 already in media/
#   3. nothing — start the API alone and say how to add a camera
#
# media/*.mp4 is gitignored, so a fresh clone legitimately has no clip. That is
# the normal state of a new test server, not an error, and hard-coding one
# machine's filename here made it look like one.
SOURCE="${CLIP:-${1:-}}"
if [ -z "$SOURCE" ]; then
  for f in media/*.mp4; do
    [ -f "$f" ] && { SOURCE="$f"; break; }
  done
fi

[ -f .local_key ] || .venv/bin/python -c "import secrets;print(secrets.token_urlsafe(32))" > .local_key
[ -f .local_integration_key ] || \
  .venv/bin/python -c "import secrets;print(secrets.token_urlsafe(32))" > .local_integration_key

export FINBLADE_API_KEY="$(cat .local_key)"
export FINBLADE_INTEGRATION_KEY="$(cat .local_integration_key)"
export FINBLADE_SITE_ID="$SITE_ID"
export FINBLADE_SELF_URL="http://127.0.0.1:$PORT"

mkdir -p scripts/logs
echo "operator key    : $FINBLADE_API_KEY"
echo "integration key : $FINBLADE_INTEGRATION_KEY"
echo "dashboard       : http://localhost:$PORT/web/dashboard.html"
echo

# --- API -------------------------------------------------------------------
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port "$PORT" \
  </dev/null >scripts/logs/api.log 2>&1 &
API_PID=$!
echo "api pid $API_PID -> scripts/logs/api.log"

for _ in $(seq 1 120); do
  curl -sf -o /dev/null -m 2 -H "Authorization: Bearer $FINBLADE_API_KEY" \
    "localhost:$PORT/api/v1/summary" && break
  sleep 1
done

# --- cameras ---------------------------------------------------------------
AUTH=(-H "Authorization: Bearer $FINBLADE_API_KEY")
JSON=(-H "Content-Type: application/json")

if [ -n "$SOURCE" ]; then
  # An explicit source was given. Register it through the API so the pipeline is
  # launched and owned exactly as it would be from the Cameras page.
  echo "== registering $CAMERA_ID -> $SOURCE =="
  curl -s -m 30 -X POST "localhost:$PORT/api/v1/cameras" "${AUTH[@]}" "${JSON[@]}" \
    -d "{\"camera_id\":\"$CAMERA_ID\",\"site_id\":\"$SITE_ID\",\"source\":\"$SOURCE\"}"
  echo
else
  # No source given: start the cameras that are ALREADY registered.
  #
  # Nothing restarts them automatically. The API records cameras in SQLite and
  # launches a worker when one is added, but on restart it only brings up the
  # offline monitor, the report scheduler and the forwarder — the pipelines stay
  # down until something asks for them. Every camera then reads OFFLINE with its
  # row intact, which looks like the cameras failed rather than like nothing
  # started them.
  echo "== starting cameras already registered in the database =="
  IDS=$(curl -s -m 20 "${AUTH[@]}" "localhost:$PORT/api/v1/cameras" \
        | .venv/bin/python -c "
import json, sys
try:
    cams = json.load(sys.stdin).get('cameras', [])
except Exception:
    cams = []
# 'source' is credential-masked in the response, but its presence is what
# matters — /start re-reads the real value server-side.
print(' '.join(c['camera_id'] for c in cams
                if c.get('source') and c.get('enabled') is not False))
" 2>/dev/null)

  if [ -z "$IDS" ]; then
    cat <<EOF
  none found.

  Add one at  http://<host>:$PORT/web/cameras.html  (id + RTSP URL), or:

    curl -X POST localhost:$PORT/api/v1/cameras \\
      -H "Authorization: Bearer \$FINBLADE_API_KEY" \\
      -H 'Content-Type: application/json' \\
      -d '{"camera_id":"CAM-01","site_id":"$SITE_ID","source":"rtsp://user:pass@host:554/stream"}'

  To replay a local clip instead:  CLIP=media/your.mp4 bash scripts/start_demo.sh
EOF
  else
    for cid in $IDS; do
      printf '  %-14s ' "$cid"
      curl -s -m 30 -o /dev/null -w '%{http_code}\n' -X POST \
        "localhost:$PORT/api/v1/cameras/$(printf %s "$cid" | sed 's/ /%20/g')/start" \
        "${AUTH[@]}" "${JSON[@]}" -d '{}'
    done
  fi
fi

cat <<EOF

Loading the model takes ~20-40s on CPU before the first frame appears.
  watch    tail -f scripts/cam_*.log
  check    bash scripts/verify_demo.sh
  stop     bash scripts/stop_demo.sh

  Dashboard http://<host>:$PORT/web/dashboard.html
EOF
wait $API_PID
