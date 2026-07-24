#!/usr/bin/env bash
# One-command live demo: API (in-memory) + inference posting live data.
# Open the dashboard at:  http://localhost:8000/web/dashboard.html
# Ctrl-C stops the inference; the API is stopped on exit too.
set -e
cd /home/usv/finblade-cctv

CONFIG="${1:-config/cameras.synthetic.yaml}"

echo "[demo] starting API on :8000 ..."
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 \
  --log-level warning > scripts/api.log 2>&1 &
API_PID=$!
trap "echo; echo '[demo] stopping'; kill $API_PID 2>/dev/null" EXIT

# wait for API to answer
for i in $(seq 1 20); do
  if .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/zones/state',timeout=1)" 2>/dev/null; then
    break
  fi
  sleep 1
done

echo "[demo] API up. Open  ->  http://localhost:8000/web/dashboard.html"
echo "[demo] starting inference on $CONFIG (Ctrl-C to stop) ..."
# No --seconds: loops the clip until you Ctrl-C. MJPEG feed on :8080.
.venv/bin/python services/inference/run_cpu.py \
  --config "$CONFIG" --api-url http://127.0.0.1:8000
