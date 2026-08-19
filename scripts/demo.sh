#!/usr/bin/env bash
# One-command live demo: API (SQLite history) + two camera feeds posting live.
#   Dashboard:  http://localhost:8000/web/dashboard.html
#   History:    http://localhost:8000/web/history.html
# Ctrl-C stops everything.
set -e
cd /home/usv/finblade-cctv

echo "[demo] starting API on :8000 ..."
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 \
  --log-level warning > scripts/api.log 2>&1 &
API_PID=$!
CAMA_PID=""; CAMB_PID=""
cleanup(){ echo; echo "[demo] stopping"; kill $API_PID $CAMA_PID $CAMB_PID 2>/dev/null; }
trap cleanup EXIT

for i in $(seq 1 20); do
  if .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/zones/state',timeout=1)" 2>/dev/null; then break; fi
  sleep 1
done
echo "[demo] API up."
echo "[demo]   Dashboard -> http://localhost:8000/web/dashboard.html"
echo "[demo]   Cameras   -> http://localhost:8000/web/cameras.html"
echo "[demo]   History   -> http://localhost:8000/web/history.html"
echo "[demo]   Report    -> http://localhost:8000/web/report.html"

echo "[demo] starting Camera A (:8080, synthetic scenario) ..."
.venv/bin/python services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --port 8080 --stream-host localhost --api-url http://127.0.0.1:8000 > scripts/camA.log 2>&1 &
CAMA_PID=$!

echo "[demo] starting Camera B (:8081, terminal clip) ..."
.venv/bin/python services/inference/run_cpu.py --config config/cameras.cam2.yaml \
  --port 8081 --stream-host localhost --api-url http://127.0.0.1:8000 > scripts/camB.log 2>&1 &
CAMB_PID=$!

echo "[demo] running.  Camera A pid=$CAMA_PID   Camera B pid=$CAMB_PID"
echo "[demo] camera-offline demo:  kill $CAMB_PID   (R-07 fires within ~30s)"
echo "[demo] Ctrl-C to stop."
wait
