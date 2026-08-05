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
CLIP="${CLIP:-media/1903279-uhd_1920_1440_30fps.mp4}"

[ -f .local_key ] || .venv/bin/python -c "import secrets;print(secrets.token_urlsafe(32))" > .local_key
[ -f .local_integration_key ] || \
  .venv/bin/python -c "import secrets;print(secrets.token_urlsafe(32))" > .local_integration_key

export FINBLADE_API_KEY="$(cat .local_key)"
export FINBLADE_INTEGRATION_KEY="$(cat .local_integration_key)"
export FINBLADE_SITE_ID="$SITE_ID"
export FINBLADE_SELF_URL="http://127.0.0.1:$PORT"

if [ ! -f "$CLIP" ]; then
  echo "[BLOCKER] no video at $CLIP — set CLIP=<path> and retry" >&2
  exit 2
fi

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

# --- camera pipeline -------------------------------------------------------
# Registered through the API with a source, so the API launches and owns the
# worker exactly as it would for a camera added from the Cameras page.
curl -s -m 20 -X POST "localhost:$PORT/api/v1/cameras" \
  -H "Authorization: Bearer $FINBLADE_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"camera_id\":\"$CAMERA_ID\",\"site_id\":\"$SITE_ID\",\"name\":\"Demo camera\",\"source\":\"$CLIP\"}"
echo
echo
echo "Loading YOLO weights takes ~20-40s on CPU before the first frame appears."
echo "Watch it come up:  tail -f scripts/cam_${CAMERA_ID}.log"
echo "Stop everything :  bash scripts/stop_demo.sh"
echo
wait $API_PID
