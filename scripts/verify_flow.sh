#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1; rm -f data/finblade.db
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 22 --no-serve --api-url http://127.0.0.1:8000 > scripts/pf.log 2>&1
.venv/bin/python - <<'PY'
import json, urllib.request
zs=json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/zones/state",timeout=3).read())["zones"]
for z in zs:
    print(f"  {z['zone_id']:15} in1m={z.get('inflow_per_min')} net={z.get('net_flow')} "
          f"in5m={z.get('inflow_5m')} in15m={z.get('inflow_15m')} peak={z.get('peak_occupancy')}")
PY
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
