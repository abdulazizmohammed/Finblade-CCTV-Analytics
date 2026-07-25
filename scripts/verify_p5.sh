#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1
rm -f data/finblade.db
.venv/bin/python - <<'PY'
import yaml
d = yaml.safe_load(open("config/cameras.synthetic.yaml"))
d["offline_seconds"] = 6; d["process_fps"] = 15
yaml.safe_dump(d, open("config/_p5.yaml", "w"))
PY
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
.venv/bin/python -u services/inference/run_cpu.py --config config/_p5.yaml \
  --api-url http://127.0.0.1:8000 > scripts/p5.log 2>&1 &
RUN=$!
hit(){ .venv/bin/python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080$1',timeout=3).read().decode())"; }
sleep 15
echo "=== simulate camera failure ==="; hit /simulate-failure
sleep 9
echo "=== restore ==="; hit /restore
sleep 6
kill -9 $RUN 2>/dev/null
.venv/bin/python - <<'PY'
import json, time, urllib.request
from collections import Counter
g=lambda p: json.loads(urllib.request.urlopen("http://127.0.0.1:8000"+p,timeout=3).read())
ev=g(f"/api/v1/history/events?from=0&to={time.time()+1e6}&limit=5000")["events"]
print("event types:", dict(Counter(e['event_type'] for e in ev)))
zs=g("/api/v1/zones/state")["zones"]
for z in zs:
    print(f"  {z['zone_id']:15} net_flow={z.get('net_flow')} in1m={z.get('inflow_per_min')} "
          f"in5m={z.get('inflow_5m')} in15m={z.get('inflow_15m')}")
mv=g("/api/v1/movement?minutes=15")["flows"]
print("movement flows:", [(f['zone_from'],f['zone_to'],f['count']) for f in mv][:6])
PY
grep -iE 'error|traceback' scripts/p5.log | head -3 || true
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
rm -f config/_p5.yaml
