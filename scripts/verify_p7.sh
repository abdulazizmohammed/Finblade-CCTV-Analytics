#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1; rm -f data/finblade.db
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 40 --no-serve --api-url http://127.0.0.1:8000 > scripts/p7.log 2>&1
.venv/bin/python - <<'PY'
import json, time, urllib.request
from collections import Counter
g=lambda p: json.loads(urllib.request.urlopen("http://127.0.0.1:8000"+p,timeout=3).read())
ev=g(f"/api/v1/history/events?from=0&to={time.time()+1e6}&limit=8000")["events"]
et=Counter(e['event_type'] for e in ev)
print("EVENTS:", {k:et[k] for k in sorted(et)})
rex=[e for e in ev if e['event_type']=='RESTRICTED_ZONE_EXIT']
durs=[e.get('duration') for e in rex]
have=[d for d in durs if isinstance(d,(int,float))]
print(f"RESTRICTED_ZONE_EXIT: {len(rex)}  with-duration: {len(have)}")
if have:
    print(f"  duration s  min={min(have):.1f}  max={max(have):.1f}  sample={have[:6]}")
al=g(f"/api/v1/history/alerts?from=0&to={time.time()+1e6}&limit=8000")["alerts"]
byrule=Counter(a['rule_id'] for a in al)
print("ALERTS:", {k:byrule[k] for k in sorted(byrule)})
PY
echo "--- errors ---"; grep -iE 'error|traceback' scripts/p7.log | head -3 || true
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
