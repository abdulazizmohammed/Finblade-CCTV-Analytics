#!/usr/bin/env bash
cd /home/usv/finblade-cctv
pkill -f 'run_cpu.py|uvicorn services.api' 2>/dev/null; sleep 2
nohup .venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
nohup .venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --source media/CAM01_S01_normal_entry_exit.mp4 --port 8080 --stream-host localhost \
  --api-url http://127.0.0.1:8000 > scripts/cam01.log 2>&1 &
sleep 13
echo "=== live zones for CAM-SYN-01 (expect NONE) ==="
.venv/bin/python - <<'PY'
import json,urllib.request
zs=json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/zones/state",timeout=5).read())["zones"]
print("  ", [z["zone_id"] for z in zs if z.get("camera_id")=="CAM-SYN-01"])
PY
echo "=== detection running ==="; tail -2 scripts/cam01.log
curl -s -o /dev/null -w "dashboard -> %{http_code}\n" http://127.0.0.1:8000/web/dashboard.html
echo "=== errors ==="; grep -iE 'error|traceback' scripts/cam01.log | head -3 || true
rm -f scripts/_final.sh
