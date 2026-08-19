#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1
rm -f data/finblade.db evidence/bookmarks/*.jpg
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
echo "[v] running synthetic 25s on GPU ..."
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 25 --no-serve --api-url http://127.0.0.1:8000 > scripts/vlog.log 2>&1
.venv/bin/python - <<'PY'
import json, time, urllib.request
from collections import Counter
now=time.time()
g=lambda p: json.loads(urllib.request.urlopen("http://127.0.0.1:8000"+p,timeout=3).read())
al=g(f"/api/v1/history/alerts?from=0&to={now+1e6}&limit=500")["alerts"]
ev=g(f"/api/v1/history/events?from=0&to={now+1e6}&limit=1000")["events"]
byrule=Counter(a["rule_id"] for a in al)
frame=Counter(a["rule_id"] for a in al if a.get("frame"))
print("ALERTS  (rule: total / with-frame):")
for r in sorted(byrule): print(f"  {r}: {byrule[r]} / {frame[r]}")
print("EVENTS with frame:", sum(1 for e in ev if e.get('frame')), "/", len(ev))
PY
echo "[v] bookmark files on disk:"; ls evidence/bookmarks/ 2>/dev/null | wc -l
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
