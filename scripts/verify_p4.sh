#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1
rm -f data/finblade.db
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 25 --no-serve --api-url http://127.0.0.1:8000 > scripts/rp4.log 2>&1
.venv/bin/python - <<'PY'
import json, urllib.request
g=lambda p: json.loads(urllib.request.urlopen("http://127.0.0.1:8000"+p,timeout=3).read())
zs=g("/api/v1/zones/state")["zones"]
print("=== live zone-state (status vocab + peak/avg/trend) ===")
for z in zs:
    print(f"  {z['zone_id']:15} status={z.get('status'):8} occ={z.get('occupancy'):>2} "
          f"dens={float(z.get('density',0)):.2f} peak={z.get('peak_occupancy')} "
          f"avg={z.get('avg_occupancy')} trend={z.get('trend')}")
al=g("/api/v1/history/alerts?from=0&to=99999999999")["alerts"]
from collections import Counter
print("alerts by rule:", dict(Counter(a['rule_id'] for a in al)))
PY
grep -iE 'error|traceback' scripts/rp4.log | head -3 || true
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
