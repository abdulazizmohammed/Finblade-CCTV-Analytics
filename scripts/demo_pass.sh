#!/usr/bin/env bash
# FinBlade automated demo pass — runs the scripted end-to-end flow against the
# synthetic clip and prints a step-by-step summary. Doubles as the Phase 10 live
# verification (report scheduler + CSV + on-demand + alert lifecycle + camera sim).
set -u
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1; rm -f data/finblade.db

# Short scheduled-report cadence so R-08 fires during the demo (prod default 3600).
FINBLADE_REPORT_INTERVAL=15 .venv/bin/python -m uvicorn services.api.app:app \
  --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4

echo "[1] starting inference on the synthetic clip (CUDA if available)…"
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 45 --port 8090 --stream-host 127.0.0.1 --api-url http://127.0.0.1:8000 > scripts/demo.log 2>&1 &
RUNNER=$!
sleep 22   # let detections, zone events, density + alerts accumulate

.venv/bin/python - <<'PY'
import json, time, urllib.request
from collections import Counter
B="http://127.0.0.1:8000"
def req(path, method="GET", body=None):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(B+path, data=data, method=method,
                             headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=5).read())

ev=req(f"/api/v1/history/events?from=0&to={time.time()+1e6}&limit=9000")["events"]
print("[2] events:", dict(sorted(Counter(e['event_type'] for e in ev).items())))

feed=req("/api/v1/alerts")["alerts"]
print(f"[3] active alerts: {len(feed)}", dict(Counter(a['rule_id'] for a in feed)))

if feed:
    aid=feed[0]["alert_id"]
    print("[4] resolve one alert ->", req(f"/api/v1/alerts/{aid}/resolve","POST",
          {"action":"RESOLVED","resolved_by":"demo-operator","note":"reviewed on CCTV"})["status"])

print("[5] simulate camera failure ->", req("/api/v1/cameras/CAM-SYN-01/simulate-failure","POST")["simulate"])
time.sleep(6)
print("    restore camera        ->", req("/api/v1/cameras/CAM-SYN-01/restore","POST")["simulate"])

rep=req("/api/v1/reports/generate","POST",{"from":0,"to":time.time()+1e6})
print(f"[6] on-demand report #{rep['report_id']}: zones={rep['totals']['zones']} "
      f"peak_occ={rep['totals']['peak_total_occupancy']} alerts={rep['totals']['total_alerts']}")

csv=urllib.request.urlopen(B+"/api/v1/reports/occupancy.csv?from=0&to=%d"%(time.time()+1e6)).read().decode()
print("[7] CSV export (first 2 lines):")
for ln in csv.strip().splitlines()[:2]: print("     ", ln)
PY

echo "[8] waiting for a scheduled (R-08) report to fire…"
sleep 12
.venv/bin/python - <<'PY'
import json, urllib.request
from collections import Counter
B="http://127.0.0.1:8000"
g=lambda p: json.loads(urllib.request.urlopen(B+p,timeout=5).read())
reps=g("/api/v1/reports")["reports"]
print("    reports on record:", dict(Counter(r['kind'] for r in reps)))
for c in g("/api/v1/cameras")["cameras"]:
    print(f"[9] camera {c['camera_id']}: state={c['effective_state']} fps={c.get('input_fps')}")
PY
echo "--- runner errors ---"; grep -iE 'error|traceback' scripts/demo.log | head -3 || true
echo "[10] evidence: evidence/contact_CAM-SYN-01.jpg  evidence/metrics_CAM-SYN-01.json"
kill -9 $RUNNER 2>/dev/null
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
