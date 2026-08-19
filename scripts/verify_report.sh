#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1
rm -f data/finblade.db
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
echo "[v] populating ~20s of zone-state ..."
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 20 --no-serve --api-url http://127.0.0.1:8000 > scripts/vlog.log 2>&1
.venv/bin/python - <<'PY'
import json, time, urllib.request
now=time.time()
g=lambda p: json.loads(urllib.request.urlopen("http://127.0.0.1:8000"+p,timeout=3).read())
rep=g(f"/api/v1/reports/occupancy.json?from=0&to={now+1e6}")
print("report totals:", rep["totals"])
for z in rep["zones"]:
    print(f"  {z['zone_id']} ({z['zone_name']}) cam={z['camera_id']} "
          f"avg_occ={z['avg_occupancy']:.1f} peak={z['peak_occupancy']} "
          f"avg_den={z['avg_density']:.2f} samples={z['samples']}")
# filter test: restrict to one zone
one=g(f"/api/v1/reports/occupancy.json?from=0&to={now+1e6}&zone_id=ZONE-RESTRICTED")
print("filter zone=ZONE-RESTRICTED ->", [z['zone_id'] for z in one['zones']])
# pages served
for pth in ["/web/report.html","/web/dashboard.html"]:
    s=urllib.request.urlopen("http://127.0.0.1:8000"+pth,timeout=3)
    print(s.status, pth, len(s.read()), "bytes")
PY
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
