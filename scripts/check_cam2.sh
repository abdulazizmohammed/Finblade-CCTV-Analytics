#!/usr/bin/env bash
cd /home/usv/finblade-cctv
# ensure API is up (start if not)
if ! .venv/bin/python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/zones/state',timeout=1)" 2>/dev/null; then
  .venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
  sleep 4
fi
echo "[cam2] running Camera B for 20s ..."
.venv/bin/python services/inference/run_cpu.py --config config/cameras.cam2.yaml \
  --port 8081 --seconds 20 --api-url http://127.0.0.1:8000 > scripts/camB.log 2>&1
echo "[cam2] done. tail camB.log:"; tail -3 scripts/camB.log
.venv/bin/python - <<'PY'
import json,urllib.request
def g(p): return json.loads(urllib.request.urlopen("http://127.0.0.1:8000"+p,timeout=3).read())
cams=g("/api/v1/cameras")["cameras"]
print("cameras:", [(c["camera_id"], c.get("online")) for c in cams])
zs=g("/api/v1/zones/state")["zones"]
print("zones:", [(z.get("camera_id"), z.get("zone_id"), z.get("occupancy")) for z in zs])
PY
