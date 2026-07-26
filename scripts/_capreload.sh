#!/usr/bin/env bash
cd /home/usv/finblade-cctv
pkill -f 'run_cpu.py|uvicorn services.api' 2>/dev/null; sleep 2
nohup .venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
# seed an editor zone with capacity 40
.venv/bin/python - <<'PY'
import json,urllib.request
r=urllib.request.Request("http://127.0.0.1:8000/api/v1/zones",data=json.dumps({"camera_id":"CAM-SYN-01","zones":[
 {"zone_id":"ROOM","zone_name":"Room","zone_type":"MONITORED","restricted":False,"capacity_max":40,
  "area_sqm":30,"warning_density":2,"critical_density":4,"loitering_threshold_sec":30,"colour":"#4fdce0",
  "adjacency_list":[],"enabled":True,"normalized_polygon":[[0.05,0.2],[0.95,0.2],[0.95,0.95],[0.05,0.95]]}]}).encode(),
  method="POST",headers={"Content-Type":"application/json"})
print("seed cap40:",json.loads(urllib.request.urlopen(r,timeout=5).read()))
PY
nohup .venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --source media/CAM01_S01_normal_entry_exit.mp4 --port 8080 --stream-host localhost \
  --api-url http://127.0.0.1:8000 > scripts/cam01.log 2>&1 &
sleep 13
echo "=== zone-state capacity BEFORE (expect 40) ==="
.venv/bin/python - <<'PY'
import json,urllib.request
zs=json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/zones/state",timeout=5).read())["zones"]
for z in zs:
    if z.get("camera_id")=="CAM-SYN-01": print("  ",z["zone_id"],"capacity_max=",z.get("capacity_max"),"occ=",z.get("occupancy"),"cap%=",z.get("capacity_pct"))
PY
echo "=== change capacity to 10 (like the card edit) ==="
.venv/bin/python - <<'PY'
import json,urllib.request
r=urllib.request.Request("http://127.0.0.1:8000/api/v1/zones",data=json.dumps({"camera_id":"CAM-SYN-01","zones":[
 {"zone_id":"ROOM","zone_name":"Room","zone_type":"MONITORED","restricted":False,"capacity_max":10,
  "area_sqm":30,"warning_density":2,"critical_density":4,"loitering_threshold_sec":30,"colour":"#4fdce0",
  "adjacency_list":[],"enabled":True,"normalized_polygon":[[0.05,0.2],[0.95,0.2],[0.95,0.95],[0.05,0.95]]}]}).encode(),
  method="POST",headers={"Content-Type":"application/json"})
print("save cap10:",json.loads(urllib.request.urlopen(r,timeout=5).read()))
PY
sleep 10
echo "=== zone-state capacity AFTER (expect 10) ==="
.venv/bin/python - <<'PY'
import json,urllib.request
zs=json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/zones/state",timeout=5).read())["zones"]
for z in zs:
    if z.get("camera_id")=="CAM-SYN-01": print("  ",z["zone_id"],"capacity_max=",z.get("capacity_max"),"occ=",z.get("occupancy"),"cap%=",z.get("capacity_pct"))
PY
grep -iE 'hot-reloaded' scripts/cam01.log | tail -2
rm -f scripts/_capreload.sh
