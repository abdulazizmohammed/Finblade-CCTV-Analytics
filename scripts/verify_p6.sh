#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1; rm -f data/finblade.db
.venv/bin/python - <<'PY'
import yaml
d = yaml.safe_load(open("config/cameras.synthetic.yaml"))
for z in d["zones"]:
    z["loitering_threshold_sec"] = 3      # low, so loitering fires in a short run
d["process_fps"] = 15
yaml.safe_dump(d, open("config/_p6.yaml", "w"))
PY
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
.venv/bin/python -u services/inference/run_cpu.py --config config/_p6.yaml \
  --seconds 30 --no-serve --api-url http://127.0.0.1:8000 > scripts/p6.log 2>&1
.venv/bin/python - <<'PY'
import json, time, urllib.request
from collections import Counter
g=lambda p: json.loads(urllib.request.urlopen("http://127.0.0.1:8000"+p,timeout=3).read())
al=g(f"/api/v1/history/alerts?from=0&to={time.time()+1e6}&limit=5000")["alerts"]
byrule=Counter(a['rule_id'] for a in al)
withframe=Counter(a['rule_id'] for a in al if a.get('frame'))
print("ALERTS  rule: total / with-snapshot")
for r in sorted(byrule): print(f"  {r}: {byrule[r]} / {withframe[r]}")
ev=g(f"/api/v1/history/events?from=0&to={time.time()+1e6}&limit=5000")["events"]
et=Counter(e['event_type'] for e in ev)
print("LOITERING_START:", et.get('LOITERING_START',0), " LOITERING_END:", et.get('LOITERING_END',0))
PY
grep -iE 'error|traceback' scripts/p6.log | head -3 || true
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
rm -f config/_p6.yaml
