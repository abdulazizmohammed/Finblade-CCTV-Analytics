#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1
rm -f data/finblade.db
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4

echo "=== save synthetic zones to API (as the editor would), then read back ==="
.venv/bin/python - <<'PY'
import json, urllib.request
from finblade.config import load_camera_config
c = load_camera_config("config/cameras.synthetic.yaml")
zones = [{
    "zone_id": z.zone_id, "zone_name": z.zone_name, "zone_type": z.zone_type,
    "restricted": z.restricted, "capacity_max": z.capacity_max, "area_sqm": z.area_sqm,
    "warning_density": z.warning_density, "critical_density": z.critical_density,
    "loitering_threshold_sec": z.loitering_threshold_sec, "adjacency_list": z.adjacency_list,
    "colour": z.colour, "enabled": True,
    "normalized_polygon": [[round(x/c.frame_width, 5), round(y/c.frame_height, 5)] for x, y in z.polygon],
} for z in c.zones]
body = json.dumps({"camera_id": c.camera_id, "zones": zones}).encode()
req = urllib.request.Request("http://127.0.0.1:8000/api/v1/zones", data=body,
                            headers={"Content-Type": "application/json"})
print("POST ->", json.loads(urllib.request.urlopen(req, timeout=3).read()))
got = json.loads(urllib.request.urlopen(
    "http://127.0.0.1:8000/api/v1/zones?camera_id=" + c.camera_id, timeout=3).read())["zones"]
print("GET  ->", len(got), "zones:", [(z["zone_id"], z["zone_type"], len(z["normalized_polygon"])) for z in got])
PY

echo "=== run runner with --api-url; it should LOAD zones from API ==="
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 18 --no-serve --api-url http://127.0.0.1:8000 > scripts/rz.log 2>&1
grep -E 'loaded .* zone|using .* zone' scripts/rz.log
echo "tracked frames: $(grep -c tracked= scripts/rz.log)   alerts fired: $(grep -cE 'R-0' evidence/alerts_CAM-SYN-01.jsonl 2>/dev/null)"

echo "=== pages served ==="
.venv/bin/python - <<'PY'
import urllib.request
for p in ["/tools/zone-editor.html", "/api/v1/zones?camera_id=CAM-SYN-01"]:
    r = urllib.request.urlopen("http://127.0.0.1:8000"+p, timeout=3)
    print(r.status, p, len(r.read()), "bytes")
PY
grep -iE 'error|traceback' scripts/rz.log | head -3 || true
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
