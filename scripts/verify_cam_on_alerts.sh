#!/usr/bin/env bash
cd /home/usv/finblade-cctv
# fresh start
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1
rm -f data/finblade.db
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
echo "[v] running synthetic 20s on GPU, posting to API ..."
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 20 --no-serve --api-url http://127.0.0.1:8000 > scripts/vlog.log 2>&1
.venv/bin/python - <<'PY'
import json, time, urllib.request
now=time.time()
al=json.loads(urllib.request.urlopen(f"http://127.0.0.1:8000/api/v1/history/alerts?from=0&to={now+1e6}&limit=500",timeout=3).read())["alerts"]
print("total alerts:", len(al))
from collections import Counter
byrule=Counter(a["rule_id"] for a in al)
withcam=Counter(a["rule_id"] for a in al if a.get("camera_id"))
for r in sorted(byrule):
    print(f"  {r}: {byrule[r]} alerts, {withcam[r]} with camera_id")
crit=[a for a in al if a["rule_id"]=="R-06"][:3]
for a in crit:
    print("  R-06 sample -> camera_id:", a.get("camera_id"), "| zone:", a.get("zone_id"), "| frame:", bool(a.get("frame")))
PY
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
