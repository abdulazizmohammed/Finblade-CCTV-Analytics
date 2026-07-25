#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1; rm -f data/finblade.db
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4

echo "=== alert lifecycle: raise -> feed -> resolve -> gone -> history ==="
.venv/bin/python - <<'PY'
import json, urllib.request
def req(path, method="GET", body=None):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request("http://127.0.0.1:8000"+path, data=data, method=method,
                             headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=3).read())
aid=req("/api/v1/alerts",  "POST", {"rule_id":"R-06","severity":"RED",
        "message":"intrusion CAM-A","camera_id":"CAM-A-01","zone_id":"Z1","ts":100.0})["alert_id"]
print("  raised alert_id:", aid)
feed=req("/api/v1/alerts")["alerts"]
print("  active feed:", [(a['alert_id'],a['status']) for a in feed])
print("  resolve ->", req(f"/api/v1/alerts/{aid}/resolve","POST",
      {"action":"RESOLVED","resolved_by":"operator","note":"checked CCTV, cleared"}))
feed=req("/api/v1/alerts")["alerts"]
print("  active feed after resolve:", [(a['alert_id'],a['status']) for a in feed])
hist=req("/api/v1/history/alerts?from=0&to=1e12")["alerts"]
h=[a for a in hist if a['alert_id']==aid][0]
print(f"  history row: status={h['status']} note={h.get('note')!r} by={h.get('resolved_by')}")
PY

echo "=== overlay-toggle stream (?zones=0&ids=0&feet=0) yields valid JPEG ==="
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 25 --port 8090 --stream-host 127.0.0.1 --api-url http://127.0.0.1:8000 > scripts/p9.log 2>&1 &
RUNNER=$!
sleep 12
.venv/bin/python - <<'PY'
import urllib.request
raw=urllib.request.urlopen("http://127.0.0.1:8090/stream?zones=0&ids=0&feet=0&boxes=1",timeout=6)
buf=raw.read(6000)
soi=buf.find(b"\xff\xd8")   # JPEG start-of-image marker
print(f"  bytes={len(buf)} has_multipart={b'--frame' in buf} has_jpeg_SOI={soi>=0}")
PY
echo "--- runner errors ---"; grep -iE 'error|traceback' scripts/p9.log | head -3 || true
kill -9 $RUNNER 2>/dev/null
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
